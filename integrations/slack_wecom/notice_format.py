"""有界的分项通知数据格式与固定来源发布清单读取。"""
from datetime import date
from contextlib import contextmanager
import hashlib
import json
import re
import signal
import threading
import urllib.error
import urllib.parse
import urllib.request

MAX_NOTICE_BYTES = 1900
MAX_MANIFEST_BYTES = 32768
SHA = re.compile(r"^[a-f0-9]{40}$")
HASH = re.compile(r"^[a-f0-9]{64}$")
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class NoticeReadError(Exception):
    """投递前公开读取失败；保留原消息游标，不跳过待处理消息。"""


def require(condition):
    if not condition:
        raise ValueError("invalid_article_notice")


def exact(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys))


def day_string(day):
    try:
        valid = isinstance(day, str) and date.fromisoformat(day).isoformat() == day
    except ValueError:
        valid = False
    require(valid)
    return day


def base_url(base):
    require(isinstance(base, str))
    u = urllib.parse.urlsplit(base)
    require(u.scheme == "https" and u.hostname and not u.username and not u.password
            and u.port in (None, 443) and not u.query and not u.fragment and base.endswith("/")
            and not any(c.isspace() or ord(c) < 32 or c in "<>&\\" for c in base))
    return base


def article_text(day, revision, articles, base):
    day_string(day)
    base_url(base)
    require(type(revision) is int and 0 < revision <= 9007199254740991 and isinstance(articles, list) and 0 < len(articles) <= 30)
    root = f"{base}{day}/r{revision}/"
    lines = [f"AI Infra 晨报 · {day}"]
    seen = set()
    for number, article in enumerate(articles, 1):
        require(isinstance(article, dict))
        identity, title = article.get("id"), article.get("title")
        require(isinstance(identity, str) and ID.fullmatch(identity) and identity != "index" and identity not in seen)
        require(isinstance(title, str) and title.strip() == title and title and len(title.encode("utf-8")) <= 300
                and not any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "<>\u2028\u2029\ufeff" for c in title))
        seen.add(identity)
        lines.extend(["", title, root + identity + "/"])
    text = "\n".join(lines)
    require(len(text.encode("utf-8")) <= MAX_NOTICE_BYTES)
    return text


def build_manifest(report, base, report_hash, commit):
    require(isinstance(report, dict) and isinstance(commit, str) and SHA.fullmatch(commit)
            and isinstance(report_hash, str) and HASH.fullmatch(report_hash))
    day, revision = report.get("date"), report.get("revision")
    text = article_text(day, revision, report.get("articles"), base)
    root = f"{base}{day}/r{revision}/"
    articles = [{"id": a["id"], "title": a["title"], "url": root + a["id"] + "/"} for a in report["articles"]]
    poster = report.get("poster")
    require(isinstance(poster, str) and re.fullmatch(r"assets/[a-z0-9][a-z0-9-]{0,79}\.(png|jpg)", poster))
    images = report.get("images")
    require(isinstance(images, list))
    matches = [i for i in images if isinstance(i, dict) and i.get("path") == poster]
    require(len(matches) == 1 and isinstance(matches[0].get("sha256"), str) and HASH.fullmatch(matches[0]["sha256"]))
    manifest = {"schema_version": "1.0", "commit": commit, "date": day, "revision": revision,
                "index_url": root, "articles": articles, "text": text,
                "poster": {"url": f"{base}reports/{day}/r{revision}/{poster}", "sha256": matches[0]["sha256"]},
                "report_sha256": report_hash}
    validate_manifest(manifest, base)
    return manifest


def validate_manifest(manifest, base):
    exact(manifest, ["schema_version", "commit", "date", "revision", "index_url", "articles", "text", "poster", "report_sha256"])
    require(manifest["schema_version"] == "1.0" and isinstance(manifest["commit"], str) and SHA.fullmatch(manifest["commit"])
            and isinstance(manifest["report_sha256"], str) and HASH.fullmatch(manifest["report_sha256"]))
    expected = article_text(manifest["date"], manifest["revision"], manifest["articles"], base)
    root = f"{base}{manifest['date']}/r{manifest['revision']}/"
    require(manifest["index_url"] == root and manifest["text"] == expected)
    for article in manifest["articles"]:
        exact(article, ["id", "title", "url"])
        require(article["url"] == root + article["id"] + "/")
    exact(manifest["poster"], ["url", "sha256"])
    poster = manifest["poster"]
    require(isinstance(poster["url"], str) and re.fullmatch(re.escape(base + "reports/" + manifest["date"] + "/r" + str(manifest["revision"]) + "/") + r"assets/[a-z0-9][a-z0-9-]{0,79}\.(png|jpg)", poster["url"])
            and isinstance(poster["sha256"], str) and HASH.fullmatch(poster["sha256"]))
    require(len(json.dumps(manifest, ensure_ascii=False).encode("utf-8")) <= MAX_MANIFEST_BYTES)
    return manifest


def parse_article_text(text, base):
    """仅核对结构；仍须独立获得发布清单后才能认可通知。"""
    require(isinstance(text, str) and len(text.encode("utf-8")) <= MAX_NOTICE_BYTES and "\r" not in text)
    sections = text.split("\n\n")
    require(len(sections) >= 2)
    match = re.fullmatch(r"AI Infra 晨报 · (\d{4}-\d{2}-\d{2})", sections[0])
    require(match is not None)
    day = day_string(match[1])
    articles, revision = [], None
    for number, section in enumerate(sections[1:], 1):
        lines = section.split("\n")
        require(len(lines) == 2)
        url = re.fullmatch(re.escape(base + day + "/") + r"r([1-9]\d*)/([a-z0-9][a-z0-9-]{0,79})/", lines[1])
        require(url is not None)
        current = int(url[1])
        if revision is None:
            revision = current
        require(revision == current)
        articles.append({"id": url[2], "title": lines[0], "url": lines[1]})
    require(article_text(day, revision, articles, base) == text)
    return {"date": day, "revision": revision, "articles": articles, "index_url": f"{base}{day}/r{revision}/"}


def match_manifest(candidate, manifest, base):
    validate_manifest(manifest, base)
    require(candidate["text"] == manifest["text"] and candidate["url"] == manifest["index_url"]
            and candidate["articles"] == manifest["articles"])
    return manifest


@contextmanager
def public_read_deadline():
    """覆盖全部清单读取的总期限，不以每次套接字读取超时代替。"""
    if (not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread()
            or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0)):
        raise NoticeReadError("notice_read_deadline_unavailable")
    previous = signal.getsignal(signal.SIGALRM)
    def expired(signum, frame):
        raise NoticeReadError("notice_read_deadline_exceeded")
    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, 30)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class NoticeManifestReader:
    """不携带 Slack 凭据，不跟随重定向，不接受消息指定的读取网址。"""
    def __init__(self, base, read_json=None):
        self.base = base_url(base)
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.read_json = read_json or self._read_json

    def _read_json(self, url, limit):
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
            with self.http.open(request, timeout=15) as response:
                require(response.status == 200 and response.headers.get_content_type() == "application/json")
                data = response.read(limit + 1)
                require(len(data) <= limit)
                return json.loads(data.decode("utf-8"))
        except Exception:
            # 不记录远端正文、地址、凭据或底层传输异常。
            raise NoticeReadError("notice_manifest_read_retry") from None

    def read(self, candidate):
        with public_read_deadline():
            return self._read(candidate)

    def _read(self, candidate):
        identity = parse_article_text(candidate["text"], self.base)
        require(candidate["url"] == identity["index_url"])
        edition = f"{identity['date']}/r{identity['revision']}"
        try:
            before = self.read_json(self.base + "publication.json", 1048576)
            exact(before, ["schema_version", "commit", "preview", "reports"])
            require(before["schema_version"] == "1.0" and before["preview"] is False
                    and isinstance(before["commit"], str) and SHA.fullmatch(before["commit"])
                    and isinstance(before["reports"], list) and edition in before["reports"])
            manifest = self.read_json(self.base + "reports/" + edition + "/notice.json", MAX_MANIFEST_BYTES)
            validate_manifest(manifest, self.base)
            require(manifest["commit"] == before["commit"])
            after = self.read_json(self.base + "publication.json", 1048576)
            require(before == after)
            return manifest
        except NoticeReadError:
            raise
        except (TypeError, ValueError, KeyError):
            raise NoticeReadError("notice_publication_unverified") from None
