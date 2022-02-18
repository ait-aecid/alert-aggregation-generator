"""Generates and saves AMiner Meta-Alerts & Alert-Groups in elasticsearch
"""

import time
import json
import random
import itertools
import datetime
import yaml
from collections import Counter
from elasticsearch import Elasticsearch, helpers as es_helpers
from elasticsearch.exceptions import NotFoundError
#
from alertaggregation.preprocessing.objects import Alert
from alertaggregation.clustering import time_delta_group, objects as clustering_objects
from alertaggregation.merging.objects import (
    MetaAlert,
    MetaAlertManager,
    KnowledgeBase,
    Wildcard,
    Mergelist
)


class AlertEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Wildcard):
            return obj.symbol
        elif type(obj) == Mergelist:
            return obj.elements
        else:
            return obj
        return super().default(self, obj)


class MetaAlertGenerator(Elasticsearch):
    def __init__(self, **config):
        super().__init__(**config)

        if not self.connected:
            raise Exception("[x] Connection failed. Might be a configuration error, "
                "network failure or the elasticseach instance is not running.")

        self.config_keys = config.keys()
        if "alert_index" not in self.config_keys:
            raise ValueError("[x] Elasticsearch index required.")
    
        self.config = config
        self.storage = self.config.get('storage', True) # erkennen vs generieren modus
        self.query_size = 1000
        self.keep_alive = "2m"
        self.alerts_sort = [{"@timestamp": {"order": "asc"}}] # TODO: add tie_break_id
        self.meta_alerts_sort = {"@timestamp": {"order":"asc", "ignore_unmapped": True}}
        self.query = {
            "range": {
                "@timestamp": {
                    # "time_zone": "+01:00",
                    # "gte": "2021-01-21T10:00:00",
                    # "lte": "2021-01-21T18:00:00"  
                }
            }
        }

        self.daily_tag = "{:02d}.{:02d}.{:02d}".format(self.utcnow.year, self.utcnow.month, self.utcnow.day)
        self.alert_index = self.config.get('alert_index')
        self.g_alerts_base_index = "generator-alerts-"
        self.g_alert_groups_base_index = "generator-alert-groups-"
        self.g_meta_alerts_base_index = "generator-meta-alerts-"
        self.g_alert_index = self.g_alerts_base_index + '*'
        self.g_alert_groups_index = self.g_alert_groups_base_index + '*'
        self.g_meta_alert_index = self.g_meta_alerts_base_index + '*'
        self.g_alerts_daily_index = self.g_alerts_base_index + self.daily_tag
        self.g_alert_groups_daily_index = self.g_alert_groups_base_index + self.daily_tag
        self.g_meta_alerts_daily_index = self.g_meta_alerts_base_index + self.daily_tag

        self.alerts_search_after = [0]
        if "search_after" in self.config_keys and self.config.get('search_after') is not None:
            self.alerts_search_after = self.config['search_after']
            if not isinstance(self.alerts_search_after, list):
                raise ValueError("[x] search_after parameters should be in a list (timestamp @ index 0)")

        self.query_interval = self.config.get('query_interval', 30)
        alert_index_exists = self.indices.exists(index=self.alert_index)
        if not alert_index_exists:
            raise ValueError("[x] Index {} does not exist".format(self.alert_index))

        # aggregate-config
        self.noise = self.config.get('noise', 0.0)
        self.deltas = self.config.get('deltas', [0.5, 5]) # Delta time intervals for group formation in seconds.
        self.threshold = self.config.get('threshold', 0.3) # Minimum group similarity threshold for incremental clustering [0, 1].
        self.min_alert_match_similarity = self.config.get('min_alert_match_similarity', None) # Minimum alert similarity threshold for group matching [0,1]. Set to None to use same value as threshold.
        self.max_val_limit = self.config.get('max_val_limit', 10) # Maximum number of values in merge lists before they are replaced by wildcards [0, inf].
        self.min_key_occurrence = self.config.get('min_key_occurrence', 0.1) # Minimum relative occurrence frequency of attributes to be included in merged alerts [0, 1].
        self.min_val_occurrence = self.config.get('min_val_occurrence', 0.1) # Minimum relative occurrence frequency of attribute values to be included in attributes of merged alerts [0, 1].
        self.alignment_weight = self.config.get('alignment_weight', 0.1) # Influence of alignment on group similarity [0, 1].
        self.max_groups_per_meta_alert = self.config.get('max_groups_per_meta_alert', 25) # Maximum queue size [1, inf]. Set to None for unlimited queue size.
        self.queue_strategy = self.config.get('queue_strategy', 'logarithmic') # Queue storage strategy, supported strategies are 'linear' and 'logarithmic'.
        self.w = self.config.get('w', {'timestamp': 0, 'Timestamp': 0, 'timestamps': 0, 'Timestamps': 0}) # Attribute weights used in alert similarity computation. It is recommended to set the weights of timestamps to 0.
        if self.min_alert_match_similarity is None:
            self.min_alert_match_similarity = self.threshold
            
        # define kbase and manager
        self.kb = KnowledgeBase(self.max_groups_per_meta_alert, self.queue_strategy)
        self.manager = MetaAlertManager(self.kb)
        self.cached_groups = {}
        self.running = True
        self.has_processed = False
        self.frozen_timestamp = None

        meta_alerts = self.scroll_all(self.g_meta_alert_index)
        if meta_alerts:
            self.set_last_id(meta_alerts, MetaAlert)
            self.update_manager(meta_alerts) # update meta-alert manager ONLY ONCE on start

        alert_groups = self.scroll_all(self.g_alert_groups_index)

        if alert_groups:
            self.set_last_id(alert_groups, clustering_objects.Group)
            self.update_kb(alert_groups)
    

    def has_index_data(self, index):
        try:
            response = self.search(index=index, query={"match_all": {}})
            hits = response['hits']['hits']
            return len(hits)
        except NotFoundError:
            self.printout(f"Index {index} does not exist yet")

    def run(self, local_alerts=None):
        if local_alerts:
            self.generate_from_local(local_alerts)
        else:
            while self.running:
                alerts = self.fetch_alerts()
                self.frozen_timestamp = datetime.datetime.now()
                delta_groups = {}

                if alerts: 
                    delta_groups = self.group_alerts(alerts)
                else: 
                    self.printout(f"No alerts")

                self.process_and_save(delta_groups)
                self.has_processed = True
                self.printout(f"Sleeping {self.query_interval} seconds b4 the next query...")
                time.sleep(int(self.query_interval))
            

    def process_and_save(self, delta_groups):
        if delta_groups and delta_groups.values():
            self.generate_meta_alerts(delta_groups)
            # get generated alerts, alert-groups and meta-alerts
            alert_counter = itertools.count()
            
            self.g_alerts = [{
                '_index': self.g_alerts_daily_index,
                '_source': {
                    "@timestamp": self.frozen_timestamp,#alert.d.get('@timestamp'),
                    "id": next(alert_counter),
                    "d": self.serialize_alert(alert),
                    "delta": delta,
                    "groups": alert.groups_id,
                    "meta_alerts": alert.meta_alerts_id,
                }
            } for delta, groups in self.kb.delta_dict.items() for group in groups for alert in group.alerts]

            
            self.g_alert_groups = [{
                '_index': self.g_alert_groups_daily_index,
                '_source': {
                    "@timestamp": self.frozen_timestamp,
                    "id": group.id, 
                    "meta_alert_id": group.meta_alert.id,
                    "delta": delta,
                    "alerts": [self.serialize_alert(alert) for alert in group.alerts],
                    "alert_count": len(group.alerts),
                }
            } for delta, groups in self.kb.delta_dict.items() for group in groups]

            # add alert_ids into g_groups
            for group in self.g_alert_groups:
                group_id = group['_source']['id']
                for alert in self.g_alerts:
                    if group_id in alert['_source']['groups']:
                        if 'alert_ids' in group['_source']:
                            group['_source']['alert_ids'].append(alert['_source']['id'])
                        else:
                            group['_source']['alert_ids'] = [alert['_source']['id']]

            self.g_meta_alerts = self.get_meta_alerts_json()

            for meta in self.g_meta_alerts:
                meta_id = meta['_source']['id']
                for group in self.g_alert_groups:
                    if meta_id == group['_source']['meta_alert_id']:
                        group_id = group['_source']['id']
                        alert_ids = group['_source']['alert_ids']
                        if 'group_ids' in meta['_source']:
                            meta['_source']['group_ids'].append((group_id, alert_ids))
                        else:
                            meta['_source']['group_ids'] = [(group_id, alert_ids)]

            self.printout('Results:')
            for delta, meta_alerts in self.manager.meta_alerts.items():
                self.printout(' delta = ' + str(delta) + ': ' + str(len(meta_alerts)) + ' meta-alerts')    

            if self.storage:
                self.save_stats()
                # delete old alerts, alert-groups, meta-alerts and save the new/updated ones
                self.delete_by_query(index=self.g_alert_index, body={"query": {"match_all": {}}})
                self.delete_by_query(index=self.g_alert_groups_index, body={"query": {"match_all": {}}})
                self.delete_by_query(index=self.g_meta_alert_index, body={"query": {"match_all": {}}})
                time.sleep(1)
                self.save_bulk(self.g_alerts, self.g_alerts_daily_index)
                self.save_bulk(self.g_alert_groups, self.g_alert_groups_daily_index)
                self.save_bulk(self.g_meta_alerts, self.g_meta_alerts_daily_index)
            else:
                self.send_notification()

            self.config['search_after'] = self.alerts_search_after
            with open('config.yaml', 'w+') as f: yaml.dump(self.config, f)

    def fetch_alerts(self):
        self.printout(
            f"Querying alerts using search_after params: {self.alerts_search_after}"
        )
        
        if self.has_index_data(self.alert_index):
        
            pit_id = self.open_point_in_time(
                index=self.alert_index, keep_alive=self.keep_alive
            )['id']
            
            pit = {
                "id": pit_id,
                "keep_alive": self.keep_alive
            }
            
            query_body = {
                "query": self.query,
                "pit": pit,
                "search_after": self.alerts_search_after,
                "sort": self.alerts_sort,
            }
            
            _source_excludes = ["@version", "*.@logtimestamp"]
            # no need to specify index when using pit
            data = self.search(size=self.query_size, _source_excludes=_source_excludes, **query_body) 

            pit_id = data['pit_id']
            query_size = len(data['hits']['hits'])
            total_hitcount = data['hits']['total']['value']
            hits = data['hits']['hits']

            while (query_size > 0):
                # if the process is stopped abruptly, make sure last hit sort info is saved 
                # to keep track of the search_after values of the last processed alert.
                if hits: 
                    self.alerts_search_after = hits[-1]['sort']

                # re-search with new pit and search_after params
                data = self.search(size=self.query_size, _source_excludes=_source_excludes, **{
                    "query": self.query,
                    "pit": {'id': pit_id},
                    "search_after": self.alerts_search_after,
                    "sort": self.alerts_sort,
                })

                pit_id = data['pit_id']
                query_size = len(data['hits']['hits'])
                if query_size > 0:
                    hits.extend(data['hits']['hits'])

            pit_closed = self.close_point_in_time({"id": pit_id})
            return hits
    
    
    def scroll_all(self, index): 
        hits = []
        if self.has_index_data(index=index):
            data = self.search(
                index=index, 
                scroll=self.keep_alive,
                size=self.query_size,
            )

            hits = data['hits']['hits']

            qcounter = 0

            if '_scroll_id' in data.keys():
                # Get the scroll ID
                scroll_id = data['_scroll_id']
                scroll_size = len(data['hits']['hits'])
                
                while scroll_size > 0:
                    self.printout(f"Querying objects from {index} ({qcounter})")
                    data = self.scroll(
                        scroll_id=scroll_id, 
                        scroll=self.keep_alive, 
                    )
                    scroll_id = data['_scroll_id']
                    scroll_size = len(data['hits']['hits'])
                    hits.extend(data['hits']['hits'])
                    qcounter += 1

                self.clear_scroll(**{"scroll_id": scroll_id})
        self.printout(f"Found {len(hits)} existing objects")
        return hits

    def update_kb(self, groups):
        if groups:
            for group in groups:
                if '_source' in group.keys():
                    group = group['_source']

                alerts = []
                for alert in group['alerts']:
                    alert = self.deserialize_alert(alert)
                    alert = Alert(alert)
                    alert.meta_alerts_id.append(group["meta_alert_id"])
                    alert.groups_id.append(group['id'])
                    alerts.append(alert)

                group_obj = clustering_objects.Group()
                group_obj.add_to_group(alerts)
                group_obj.create_bag_of_alerts(
                    self.min_alert_match_similarity, 
                    max_val_limit=self.max_val_limit, 
                    min_key_occurrence=self.min_key_occurrence, 
                    min_val_occurrence=self.min_val_occurrence
                )
                group_obj.id = group['id']
                group_obj.delta = group['delta']

                ma_obj = None
                for delta, meta_alerts in self.manager.meta_alerts.items():
                    for meta_alert in meta_alerts:
                        if meta_alert.id == group["meta_alert_id"]:
                            ma_obj = meta_alert

                group_obj.meta_alert = ma_obj
                self.kb.add_group_delta(group_obj, group_obj.delta)

    def update_manager(self, meta_alerts): 
        # add es meta_alerts to the manager
        manager_meta_alerts = {}
        for meta_alert in meta_alerts:
            
            if '_source' in meta_alert.keys():
                meta_alert = meta_alert['_source']

            ma_obj = MetaAlert(self.manager)
            ma_obj.id = meta_alert['id']

            group_alerts = []
            # decode wildcard and mergelist!
            for alert in meta_alert['alert_group']:
                alert = self.deserialize_alert(alert['alert'])
                alert = Alert(alert)
                alert.meta_alerts_id.append(ma_obj.id)
                group_alerts.append(alert)

            group = clustering_objects.Group()
            group.add_to_group(group_alerts)
            group.create_bag_of_alerts(
                self.min_alert_match_similarity, 
                max_val_limit=self.max_val_limit, 
                min_key_occurrence=self.min_key_occurrence, 
                min_val_occurrence=self.min_val_occurrence
            )
            # group.meta_alert = ma_obj
            ma_obj.alert_group = group
            delta = meta_alert['delta']
            if delta in manager_meta_alerts:
                manager_meta_alerts[delta].append(ma_obj)
            else:
                manager_meta_alerts[delta] = [ma_obj]
            # self.kb.add_group_delta(group, delta)

        self.manager.meta_alerts = manager_meta_alerts
        if manager_meta_alerts.keys():
            self.printout(
                f"Meta-Alert Manager updated deltas {list(manager_meta_alerts.keys())}"
            )
        
    def get_meta_alerts_json(self):
        g_meta_alerts = []
        for delta, meta_alerts in self.manager.meta_alerts.items():
            for meta_alert in meta_alerts:
                g_meta_alert = {}
                g_meta_alert['delta'] = delta
                g_meta_alert['id'] = meta_alert.id
                # g_meta_alert['group_id'] = meta_alert.alert_group.id
                # g_meta_alert['alert_ids'] = [alert.id for alert in meta_alert.alert_group.alerts]
                # get alert group for each meta-alert
                alert_group_json = []
                limit = 10
                min_freq = 1
                max_freq = 1
                if (len(meta_alert.alert_group.alerts) == 0 and len(meta_alert.alert_group.bag_of_alerts) != 0) or len(meta_alert.alert_group.alerts) > limit:
                    for alert_pattern, freq in meta_alert.alert_group.bag_of_alerts.items():
                        if isinstance(freq, tuple):
                            min_freq = str(freq[0])
                            max_freq = str(freq[1])
                        else:
                            min_freq = str(freq)
                            max_freq = str(freq)
  
                        alert_group_json.append({
                            "min_frequency": min_freq,
                            "max_frequency": max_freq,
                            "alert": self.serialize_alert(alert_pattern)
                        })
                else:
                    for alert in meta_alert.alert_group.alerts:
                        alert_group_json.append({
                            "min_frequency": min_freq,
                            "max_frequency": max_freq,
                            "alert": self.serialize_alert(alert)
                        })

                g_meta_alert['alert_group'] = alert_group_json
                g_meta_alert['alert_count'] = len(alert_group_json)
                g_meta_alert['@timestamp'] = self.frozen_timestamp
                g_meta_alerts.append({
                    '_index': self.g_meta_alerts_daily_index,
                    '_source': g_meta_alert
                })
        return g_meta_alerts

    def group_alerts(self, hits=[]):
        alerts = []
        timestamps = []

        for hit in hits:
            alert = hit
            # alerts in elasticsearch are in _source of hit
            if isinstance(hit, object) and '_source' in hit.keys(): 
                alert = hit['_source']
   
            keys2del = ['@logtimestamp', '@timestamp']
            for k in keys2del:
                if k in alert.keys():
                    del alert[k]
            
            alert_obj = Alert(alert)
            alert_obj.file = "elasticsearch"
            alerts.append(alert_obj)
            timestamps.append(alert['LogData']['Timestamps'][0])

            if len(alerts) != len(timestamps):
                print('[x] Alerts and timestamps are diverging, something went wrong during alert preprocessing!')

        # Noise specifies the average amount of injected false alarms per minute, 
        # measured from first to last occurring alert
        if self.noise != 0.0:
            max_ts = max(timestamps)
            min_ts = min(timestamps)
            number_to_inject = (max_ts - min_ts) / 60.0 * noise
            sample_alerts = random.sample(alerts, min(100, len(alerts)))
            while number_to_inject > 0:
                number_to_inject -= 1
                new_alert = random.choice(sample_alerts).get_alert_clone()
                new_alert.noise = True
                alerts.append(new_alert)
                timestamps.append(random.uniform(min_ts, max_ts))

        self.deltas.sort() # deltas have to be sorted for correct group subgroups and supergroup!
        prev_groups = None
        delta_groups = {}
        cached_alerts = []

        for delta in self.deltas:
            updated_ts = timestamps
            updated_alerts = alerts

            # update timestamps and alerts with cached group alerts
            if self.cached_groups and delta in self.cached_groups.keys():
                cached_alerts = self.cached_groups[delta].alerts
                cached_ts = [alrt.d['LogData']['Timestamps'][0] for alrt in cached_alerts]
                if cached_ts:
                    updated_ts = cached_ts + updated_ts
                    updated_alerts = cached_alerts + updated_alerts

            if updated_ts:
                group_times = time_delta_group.get_time_delta_group_times(updated_ts, delta)
                groups = time_delta_group.get_groups(updated_alerts, updated_ts, group_times)

                if prev_groups is None:
                    prev_groups = groups # Initial pass, i.e., groups with smallest delta
                else:
                    time_delta_group.find_group_connections(prev_groups, groups)
                    prev_groups = groups # Use group in next iteration when delta is one step larger

                # cache last group per each delta
                self.cached_groups[delta] = groups.pop()
                delta_groups[delta] = groups
        return delta_groups

    def generate_meta_alerts(self, delta_groups):
        for delta, alert_groups in delta_groups.items():
            if alert_groups:
                self.printout("Processing alert groups with delta = " + str(delta))

            for alert_group in alert_groups:
                alert_group.create_bag_of_alerts(
                    self.min_alert_match_similarity, 
                    max_val_limit=self.max_val_limit, 
                    min_key_occurrence=self.min_key_occurrence, 
                    min_val_occurrence=self.min_val_occurrence
                )

                # return also meta_alert object to add sight_count
                new_meta_alert_generated = self.manager.add_to_meta_alerts(
                    alert_group, delta, self.threshold, 
                    min_alert_match_similarity=self.min_alert_match_similarity, 
                    max_val_limit=self.max_val_limit, 
                    min_key_occurrence=self.min_key_occurrence, 
                    min_val_occurrence=self.min_val_occurrence, 
                    w=self.w, 
                    alignment_weight=self.alignment_weight
                )

                self.kb.add_group_delta(alert_group, delta)
                new_meta_alert_info = ''
                if new_meta_alert_generated is True:
                    new_meta_alert_info = ' New meta-alert generated.'
                print(f'{self.utcnow}    Processed group ' + str(alert_groups.index(alert_group) + 1) 
                + '/'  + str(len(alert_groups)) + ' with ' + str(len(alert_group.alerts)) 
                + ' alerts.' + new_meta_alert_info)

    def generate_bulk(self, objs, index):
        out = []
        for obj in objs:
            if isinstance(obj, dict):
                obj['@timestamp'] = self.frozen_timestamp
                if "alert_group" in obj.keys():
                    for alert_group in obj['alert_group']:
                        a_obj = alert_group['alert']
                        if isinstance(a_obj, dict):
                            if "@logtimestamp" in a_obj.keys(): 
                                del alert_group['alert']['@logtimestamp']
                            if "@timestamp" in a_obj.keys(): 
                                del alert_group['alert']['@timestamp']
                out.append({
                    '_index': index,
                    '_source': json.dumps(obj)
                })
        return out

    def save_bulk(self, objs, index):
        try:
            self.printout(f"Indexing {len(objs)} objects @ {index}")
            resp = es_helpers.bulk(self, objs)
            self.printout(f"Bulk-index response: {resp}")
        except Exception as e:
            self.printout(" Error! Check logs")
            with open('log.txt', 'a+') as f: f.write('\n' + str(self.utcnow) + '\n' + repr(e))        

    def save_stats(self):
        index = 'generator-stats-' + self.daily_tag
        self.indices.create(index=index, ignore=400)

        alert_count = len(self.g_alerts)
        group_count = len(self.g_alert_groups)
        meta_count = len(self.g_meta_alerts)

        if self.has_index_data('generator-stats*'):
            data = self.search(index=index, **{
                "size": 1,
                # "sort": {"@timestamp": "desc"},
                "query": {"match_all": {}},
            })

            hits = data['hits']['hits']
            if hits:
                last_stat = hits[0]
                alert_count = alert_count - last_stat['_source']['alert_count']
                group_count = group_count - last_stat['_source']['group_count']
                meta_count = meta_count - last_stat['_source']['meta_count']

        body = {
            '@timestamp': self.frozen_timestamp,
            'alert_count': alert_count,
            'group_count': group_count,
            'meta_count': meta_count
        }
        self.index(index=index, document=body)

    def delete_redundant_meta_alerts(self):
        if self.meta_alert_es_ids:
            def gendata():
                for _index, _id in self.meta_alert_es_ids:
                    yield {
                        "_op_type": "delete",
                        "_index": _index,
                        "_id": _id,
                    }

            resp = es_helpers.bulk(self, gendata())
        self.printout(f"Deleted old meta-alerts from ES: {resp}")

    def serialize_alert(self, alert):
        return json.loads(
            json.dumps(alert.d, cls=AlertEncoder), 
            parse_int=str, 
            parse_float=str
        )

    def deserialize_alert(self, alert):
        def loop_dict(alert):
            for k, v in alert.items():
                if isinstance(v, dict):
                    loop_dict(v)
                else:
                    if isinstance(v, str) and '§' in v:
                        wc = Wildcard()
                        wc.symbol = v
                        alert[k] = wc
                    elif isinstance(v, list):
                        ml = Mergelist(v)
                        alert[k] = ml
        loop_dict(alert)
        return alert

    def set_last_id(self, objs, kls):
        if objs:
            last_id = None
            if '_source' in objs[0].keys():
                last_id = max([obj['_source']['id'] for obj in objs])
            else:
                last_id = max([obj['id'] for obj in objs])

            if last_id:
                kls.id_iter = itertools.count(start=int(last_id)+1)

    def set_last_group_id(self):
        last_group_id = None
        index_exists = self.indices.exists(index=self.g_alert_groups_index)
        if index_exists:
            data = self.search(
                index=self.g_alert_groups_index, 
                size=1,
                sort=["id:desc"]
            )
            if 'hits' in data.keys() and data['hits']['total']['value']>0:
                last_group_id = data['hits']['hits'][0]['_source']['id']

        if last_group_id:
            clustering_objects.Group.id_iter = itertools.count(start=int(last_group_id)+1)

    def set_last_meta_alert_id(self, meta_alerts):
        if meta_alerts:
            last_meta_alert_id = None
            if '_source' in meta_alerts[0].keys():
                last_meta_alert_id = max([ma['_source']['id'] for ma in meta_alerts])
            else:
                last_meta_alert_id = max([ma['id'] for ma in meta_alerts])

            if last_meta_alert_id:
                MetaAlert.id_iter = itertools.count(start=int(last_meta_alert_id)+1)
                
    def send_notification(self):
        pass

    def generate_from_local(self, alerts):
        self.frozen_timestamp = datetime.datetime.now()
        delta_groups = {}
        if alerts: 
            delta_groups = self.group_alerts(alerts)
        else: 
            self.printout(f"No new alerts")

        self.process_and_save(delta_groups)


    @property
    def connected(self):
        return self.ping()

    @property
    def utcnow(self):
        return datetime.datetime.utcnow().replace(microsecond=0).astimezone()

    @property
    def timestamp(self):
        return datetime.datetime.utcnow().astimezone().isoformat()

    def printout(self, txt):
        print(f"{self.utcnow} -- " + str(txt))



if __name__ == "__main__":
    import yaml

    try:
        with open("config.yaml", "r") as f:
            config = yaml.load(f, Loader=yaml.FullLoader)
    except FileNotFoundError as e:
        print("[INFO] -- config.yml cannot be found. Using defaults")

    g = MetaAlertGenerator(**config)
    g.run()
    
    ## if you want to process local aminer anomalies ###
    # with open ('logs.txt') as fh:
    #     alerts = fh.readlines()
    #     alerts = [json.loads(alert) for alert in alerts if 'AnalysisComponentName' in alert]
    #     g.run(alerts)