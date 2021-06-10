# Meta-Alert Generator for AMiner

## Requirements

* alertaggregation module, installed or placed in the same folder as the generator script, i.e. withing the alert-aggregation-service directory
* elasticsearch >= 7.10.1
* yaml

## Configuration

The `config.yaml` file contains the configuration variables for the generator:

- alerts_index: aminer-alerts* # index of the aminer anomalies
- deltas:
    - 0.5
    - 5
- hosts: localhost:9200 # IP and PORT of the elasticsearch search containing alerts
- query_interval: 30 # how often to query Elasticsearch for alerts
- search_after: # point-in-time for aminer alerts query
    - 0
- storage: true # save generated meta-alerts to ELASTIC
- local: false # In case the anomalies are to be processed from local sources
