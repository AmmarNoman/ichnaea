# coding=utf-8
from json import dumps
from unittest import mock

from redis import RedisError
import pytest

from ichnaea.api.exceptions import ParseError, ServiceUnavailable
from ichnaea.conftest import GEOIP_DATA
from ichnaea.models import Radio
from ichnaea.tests.factories import ApiKeyFactory
from ichnaea import util


class BaseSubmitTest(object):

    url = None
    metric_path = None
    metric_type = "submit"
    status = None

    def queue(self, celery):
        return celery.data_queues["update_incoming"]

    def _one_cell_query(self, radio=True):
        raise NotImplementedError()

    def _post(self, app, items, api_key=None, status=status, **kw):
        url = self.url
        if api_key:
            url += "?key=%s" % api_key
        extra = {"HTTP_X_FORWARDED_FOR": GEOIP_DATA["London"]["ip"]}
        result = app.post_json(
            url, {"items": items}, status=status, extra_environ=extra, **kw
        )
        return result

    def _post_one_cell(self, app, api_key=None, status=status):
        cell, query = self._one_cell_query()
        return self._post(app, [query], api_key=api_key, status=status)

    def test_gzip(self, app, celery):
        cell, query = self._one_cell_query()
        data = {"items": [query]}
        body = util.encode_gzip(dumps(data).encode())
        headers = {"Content-Encoding": "gzip"}
        res = app.post(
            self.url,
            body,
            headers=headers,
            content_type="application/json",
            status=self.status,
        )
        assert res.headers["Access-Control-Allow-Origin"] == "*"
        assert res.headers["Access-Control-Max-Age"] == "2592000"
        assert self.queue(celery).size() == 1

    def test_malformed_gzip(self, app, celery, raven):
        headers = {"Content-Encoding": "gzip"}
        app.post(
            self.url,
            "invalid",
            headers=headers,
            content_type="application/json",
            status=400,
        )
        assert self.queue(celery).size() == 0

    def test_truncated_gzip(self, app, celery, raven):
        headers = {"Content-Encoding": "gzip"}
        body = util.encode_gzip(b'{"items": []}')[:-2]
        app.post(
            self.url, body, headers=headers, content_type="application/json", status=400
        )
        assert self.queue(celery).size() == 0

    def test_bad_encoding(self, app, celery, raven):
        body = b'{"comment": "R\xe9sum\xe9 from 1990", "items": []}'
        assert "Résumé" in body.decode("iso8859-1")
        with pytest.raises(UnicodeDecodeError):
            body.decode("utf-8")
        app.post(
            self.url, body, content_type="application/json; charset=utf-8", status=400
        )
        assert self.queue(celery).size() == 0

    def test_store_sample(self, app, celery, session):
        api_key = ApiKeyFactory(store_sample_submit=0)
        session.flush()
        self._post_one_cell(app, api_key=api_key.valid_key)
        assert self.queue(celery).size() == 0

    def test_error_get(self, app, raven):
        res = app.get(self.url, status=400)
        assert res.json == ParseError().json_body()

    def test_error_empty_body(self, app, raven):
        res = app.post(self.url, "", status=400)
        assert res.json == ParseError().json_body()

    def test_error_empty_json(self, app, raven):
        res = app.post_json(self.url, {}, status=400)
        detail = {"items": "Required"}
        assert res.json == ParseError({"validation": detail}).json_body()

    def test_error_no_json(self, app, raven):
        res = app.post(self.url, "\xae", status=400)
        detail = "JSONDecodeError('Expecting value: line 1 column 1 (char 0)')"
        assert res.json == ParseError({"decode": detail}).json_body()

    def test_error_no_mapping(self, app, raven):
        res = app.post_json(self.url, [1], status=400)
        detail = {
            "": (
                '"[1]" is not a mapping type: Does not implement dict-like'
                " functionality."
            )
        }
        assert res.json == ParseError({"validation": detail}).json_body()

    def test_error_redis_failure(self, app, raven, metricsmock):
        mock_queue = mock.Mock()
        mock_queue.side_effect = RedisError()

        with mock.patch("ichnaea.queue.DataQueue.enqueue", mock_queue):
            res = self._post_one_cell(app, status=503)
            assert res.json == ServiceUnavailable().json_body()

        assert mock_queue.called
        raven.check([("ServiceUnavailable", 1)])
        assert not metricsmock.has_record("incr", "data.batch.upload")
        assert metricsmock.has_record(
            "incr",
            "request",
            value=1,
            tags=[self.metric_path, "method:post", "status:503"],
        )

    def test_log_api_key_none(self, app, redis, metricsmock):
        cell, query = self._one_cell_query()
        self._post(app, [query], api_key=None)
        assert metricsmock.has_record(
            "incr",
            self.metric_type + ".request",
            value=1,
            tags=[self.metric_path, "key:none"],
        )
        assert redis.keys("apiuser:*") == []

    def test_log_api_key_invalid(self, app, redis, metricsmock):
        cell, query = self._one_cell_query()
        self._post(app, [query], api_key="invalid_key")
        assert metricsmock.has_record(
            "incr",
            self.metric_type + ".request",
            value=1,
            tags=[self.metric_path, "key:none"],
        )
        assert redis.keys("apiuser:*") == []

    def test_log_api_key_unknown(self, app, redis, metricsmock):
        cell, query = self._one_cell_query()
        self._post(app, [query], api_key="abcdefg")
        assert metricsmock.has_record(
            "incr",
            self.metric_type + ".request",
            value=1,
            tags=[self.metric_path, "key:invalid"],
        )
        assert redis.keys("apiuser:*") == []

    def test_log_stats(self, app, redis, metricsmock):
        cell, query = self._one_cell_query()
        self._post(app, [query], api_key="test")
        assert metricsmock.has_record(
            "incr", "data.batch.upload", value=1, tags=["key:test"]
        )
        assert metricsmock.has_record(
            "incr",
            "request",
            value=1,
            tags=[self.metric_path, "method:post", "status:%s" % self.status],
        )
        assert metricsmock.has_record(
            "incr",
            self.metric_type + ".request",
            value=1,
            tags=[self.metric_path, "key:test"],
        )
        assert metricsmock.has_record(
            "timing", "request.timing", tags=[self.metric_path, "method:post"]
        )
        today = util.utcnow().date()
        assert [k.decode("ascii") for k in redis.keys("apiuser:*")] == [
            "apiuser:submit:test:%s" % today.strftime("%Y-%m-%d")
        ]

    def test_options(self, app):
        res = app.options(self.url, status=200)
        assert res.headers["Access-Control-Allow-Origin"] == "*"
        assert res.headers["Access-Control-Max-Age"] == "2592000"

    def test_radio_duplicated(self, app, celery):
        cell, query = self._one_cell_query(radio=False)
        query[self.radio_id] = Radio.gsm.name
        query[self.cells_id][0][self.radio_id] = Radio.lte.name
        self._post(app, [query])
        item = self.queue(celery).dequeue()[0]
        cells = item["report"]["cellTowers"]
        assert cells[0]["radioType"] == Radio.lte.name

    def test_radio_invalid(self, app, celery):
        cell, query = self._one_cell_query(radio=False)
        query[self.cells_id][0][self.radio_id] = "18"
        self._post(app, [query])
        item = self.queue(celery).dequeue()[0]
        assert "radioType" not in item["report"]["cellTowers"][0]

    def test_radio_missing(self, app, celery):
        cell, query = self._one_cell_query(radio=False)
        self._post(app, [query])
        item = self.queue(celery).dequeue()[0]
        assert "radioType" not in item["report"]["cellTowers"]

    def test_radio_missing_in_observation(self, app, celery):
        cell, query = self._one_cell_query(radio=False)
        query[self.radio_id] = cell.radio.name
        self._post(app, [query])
        item = self.queue(celery).dequeue()[0]
        cells = item["report"]["cellTowers"]
        assert cells[0]["radioType"] == cell.radio.name

    def test_radio_missing_top_level(self, app, celery):
        cell, query = self._one_cell_query()
        self._post(app, [query])
        item = self.queue(celery).dequeue()[0]
        cells = item["report"]["cellTowers"]
        assert cells[0]["radioType"] == cell.radio.name
