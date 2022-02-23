# Meta-Alert Generator for AMiner

## Dependencies

* elasticsearch>=7.0.0,<8.0.0
* pyyaml
* alertaggregation

Run `pip install -r requirements.txt` to install the dependencies.

Note: The alertaggregation modules *must* be placed inside a folder name `alertaggregation`. Moreover, it should also be a `package`, i.e., every folder and subfolder must contain an empty `__init__.py` module.

## Configuration

The `config.yaml` file contains the configuration variables for the generator and alertaggregation library:

- alert_index:  index of the aminer anomalies
- hosts: IP (and PORT) of the elasticsearch instance (for querying and saving)
- query_interval: how often to query elasticsearch for alerts
- search_after: point-in-time for aminer alerts query. The value in the file is updated automatically after every query. Change it to 0 only when you want to query all the anomalies in the db.
- storage: True => Save generated meta-alerts to elasticsearch; False => only display
- deltas: alertaggregation parameter

Additionally, you can add any of the parameters accepted by the `elasticsearch-py` library. The most important of these are:

- http_auth: ['elastic', 'changeme']
- scheme: 'https'
- port: 443


## How the generator works

After running `generator.py`, the generator queries for aminer anomalies in the given elasticsearch instance. If it does not find anything, it waits a defined period (query_interval) and then queries again. When it find anomalies, it processes them to generate alert-groups and meta-alerts.

In case you have local anomalies (e.g., in a file), you can process them too by putting them as a JSON list in the `generator.run(alerts)` function.

## Note

It is important that the index of AMiner anomalies *not* begin with _alert-_ or be among the following since they are reserved for the generator:

* alerts-*
* alert-groups-*
* meta-alerts-*
* generator-stats*