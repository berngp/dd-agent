import time

from checks import AgentCheck
from util import md5
from util import json

import functools

from checks.libs.httplib2 import Http, HttpLib2Error

import socket


class MesosBaseAgentCheck(object):

    def run_metric(self, mesos_check):
        mesos_check.run()
        self._load_events_into_agent(mesos_check)
        self._load_metrics_into_agent(mesos_check)

    def _load_events_into_agent(self, mesos_check):
        if not mesos_check.has_events():
            self.log.debug("No events from %s " % (mesos_check.name))
            return
        for event in mesos_check.events:
            self.event(event)

    def _load_metrics_into_agent(self, mesos_check, tags=[]):
        if not mesos_check.has_metrics():
            self.log.debug("No metrics from %s " % (mesos_check.name))
            return

        for metric in mesos_check.metrics:
            self._load_metric(mesos_check.check_namespace(), metric)

    def _load_metric(self, check_name, metric, tags=[]):
        #value = content if 'f' not in spec else spec['f'](content)
        metric_type = metric['m_type'] if 'm_type' in metric else 'gauge'
        metric_name = "mesos.%s.%s" % (check_name, metric['key'])
        metric_value = metric['value']
        metric_tags = metric['tags'] + tags if 'tags' in metric else tags

        if hasattr(self, metric_type):
            func = getattr(self, metric_type)
            func(metric_name, metric_value, tags=metric_tags)
        else:
            self.log.warn("Unable to send %s, metric of type %s " /
                          "not implemented by AgentCheck!" %
                          (metric_name, metric_type))


class MesosAgentCheck(MesosBaseAgentCheck, AgentCheck):

    def check(self, instance):

        if 'url' not in instance:
            self.log.info("Skipping instance, no url found.")
            return
        url = instance.get('url')
        tags = instance.get('tags', [])
        default_timeout = self.init_config.get('default_timeout', 5)
        timeout = float(instance.get('timeout', default_timeout))

        stateCheck = MesosMasterStateCheck(url, timeout, tags)

        self.run_metric(stateCheck)

        _tags = tags + stateCheck.metric_tags

        self.run_metric(MesosMasterStatCheck(url, timeout, _tags))


class BaseMesosCheck(object):

    def __init__(self, base_url, timeout, name, tags=[], url_suffix=None):
        """
        Initialize a new check.

        :param name: The name of the check
        """
        self.name = name
        self.url_suffix = url_suffix
        self.url = "%s/%s" % (base_url, self.url_suffix)
        self.aggregation_key = md5(self.url).hexdigest()
        self.timeout = float(timeout)
        self.events = []
        self.warnings = []
        self.tags = tags
        self.metrics = []
        self.metric_tags = []

    @property
    def metric_tag_keys(self):
        return []

    @property
    def metric_definitions(self):
        return None

    def extract_tags(self, d):
        return [
            "%s:%s" %
            (k, d[k] if k in d else None)
            for k in self.metric_tag_keys
        ]

    def process_metrics(self, d):
        self.metric_tags = self.extract_tags(d)
        spec = self.metric_definitions
        if spec is None:
            self._process_metric_default(d)
        else:
            self._process_metric_from_spec(spec, d)

    def _process_metric_from_spec(self, spec, d):
        for metric in d:
            if metric in spec:
                value = spec['f'](d[metric]) if 'f' in spec else d[metric]
                metric_type = spec['type'] if 'type' in spec else 'gauge'
                self._app_metric(metric_type)(metric, value)

    def _process_metric_default(self, d):
        for metric in d:
            self._app_metric("gauge")(metric, d[metric])

    def has_events(self):
        """
        Check whether the check has saved any events

        @return whether or not the check has saved any events
        @rtype boolean
        """
        return len(self.events) > 0

    def has_metrics(self):
        """
        Check whether the check has saved any metrics.

        @return whether or not the check has saved any metrics
        @rtype boolean
        """
        return len(self.metrics) > 0

    def _app_metric(self, m_type):
        def _m(key, value, tags=[]):
            self.metrics.append({
                "m_type": m_type,
                "key": key,
                "value": value,
                "tags": tags + self.tags + self.metric_tags
            })
        return _m

    def run(self):
        # Send a check keep-alive to the server.
        self._app_metric('gauge')('check', 1)
        timeout = self.timeout
        # Check the URL
        start_time = time.time()
        try:
            h = Http(timeout=timeout)
            resp, content = h.request(self.url, "GET")
            end_time = time.time()
        except socket.timeout as e:
            # If there's a timeout
            self.timeout_event(timeout)
            return

        except socket.error as e:
            self.connection_error_event()
            return

        except HttpLib2Error as e:
            self.connection_error_event()
            return

        timing = end_time - start_time
        self._app_metric('gauge')(
            'response_time',
            timing,
            tags=['url:%s:%s' % ('http_check', self.url)])

        if resp.status == 200:
            self.process_response(content)
        else:
            self.status_code_event(resp)

    def process_response(self, content):
        try:
            _json = json.loads(content)
            if _json is None:
                self.empty_json_error()
            else:
                self.process_metrics(_json)

        except ValueError:
            self.json_parse_error()

    def timeout_event(self):
        self._event_template(
            "url timeout",
            "[%s] timed out after [%s] seconds." % (self.url, self.timeout))

    def connection_error_event(self):
        self._event_template(
            'connection error',
            'unable to connect to [%s].' % (self.url))

    def empty_json_error(self):
        self._event_template(
            'returned an empty json',
            'the json received from url [ %s ] has no content.' % (self.url))

    def status_code_event(self, r):
        self._event_template(
            'invalid reponse code',
            '[ %s ] returned a status of [ %s ]' % (self.url, r.status))

    def _event_template(self, title, text, alert_type="error"):
        self.events.append({
            'timestamp': int(time.time()),
            'event_type': "mesos_%s" % (self.name),
            'alert_type': alert_type,
            'msg_title':  "Mesos Check:%s:%s" % (self.name, title),
            'msg_text':   text,
            'aggregation_key': self.aggregation_key
        })

    def check_namespace(self):
        return self.name


class MesosMasterStatCheck(BaseMesosCheck):
    _name = "master.stats"
    _stat_url_suffix = "master/stats.json"

    def __init__(self, base_url, timeout, tags=[]):
        super(MesosMasterStatCheck, self).__init__(
            base_url, timeout, self._name, tags, self._stat_url_suffix)


class MesosMasterStateCheck(BaseMesosCheck):
    _tags = ["version", "leader", "pid", "id", "git_branch", "git_sha"]

    _metrics_specs = {
        "activated_slaves": {},
        "slaves": {
            "f": lambda a: len(a)
        },
        "completed_frameworks": {
            "f": lambda a: len(a)
        },
        "deactivated_slaves": {},
        "failed_tasks": {},
        "finished_tasks": {},
        "killed_tasks": {},
        "lost_tasks": {},
        "staged_tasks": {},
        "started_tasks": {}
    }

    _name = "master.state"

    _stat_url_suffix = "master/state.json"

    @BaseMesosCheck.metric_tag_keys.getter
    def metric_tag_keys(self):
        return self._tags

    @BaseMesosCheck.metric_definitions.getter
    def metric_definitions(self):
        return self._metrics_specs

    def __init__(self, base_url, timeout, tags=[]):
        super(
            MesosMasterStateCheck,
            self
        ).__init__(
            base_url,
            timeout,
            self._name,
            tags,
            self._stat_url_suffix
        )

if __name__ == '__main__':
    check, instances = MesosAgentCheck.from_yaml('./conf.d/mesos_master.yaml')
    for instance in instances:
        print "\nRunning the check against url: %s" % (instance['url'])
        check.check(instance)
        if check.has_events():
            print 'Events: %s' % (check.get_events())
        print 'Metrics: %s' % (check.get_metrics())
