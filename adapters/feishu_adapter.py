"""飞书多维表格适配器。

凭证走环境变量，不落盘、不进仓库：
    FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_APP_TOKEN
    FEISHU_TABLE_TASK / FEISHU_TABLE_MEMBER / FEISHU_TABLE_PROJECT
"""
import datetime
import json
import os
import urllib.error
import urllib.request

API = "https://open.feishu.cn/open-apis"


class Feishu:
    def __init__(self, app_id=None, app_secret=None, app_token=None, tables=None):
        self.app_id = app_id or os.environ["FEISHU_APP_ID"]
        self.app_secret = app_secret or os.environ["FEISHU_APP_SECRET"]
        self.app_token = app_token or os.environ["FEISHU_APP_TOKEN"]
        self.tables = tables or {
            "task": os.environ.get("FEISHU_TABLE_TASK", ""),
            "member": os.environ.get("FEISHU_TABLE_MEMBER", ""),
            "project": os.environ.get("FEISHU_TABLE_PROJECT", ""),
        }
        self._token = None

    # ---------------- 底层 ----------------
    @property
    def token(self):
        if not self._token:
            r = self._call("/auth/v3/tenant_access_token/internal",
                           {"app_id": self.app_id, "app_secret": self.app_secret}, auth=False)
            self._token = r["tenant_access_token"]
        return self._token

    def _call(self, path, data=None, method=None, auth=True):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(API + path, data=body,
                                     method=method or ("POST" if body else "GET"))
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if auth:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=90) as f:
                res = json.loads(f.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{path} HTTP {e.code}: {e.read().decode()[:300]}")
        if res.get("code") not in (0, None):
            raise RuntimeError(f"{path} code={res.get('code')} msg={res.get('msg')}")
        return res.get("data", res)

    # ---------------- 读 ----------------
    def search(self, table_key, filter_obj=None, page_size=500):
        out, pt = [], None
        url = f"/bitable/v1/apps/{self.app_token}/tables/{self.tables[table_key]}/records/search?page_size={page_size}"
        while True:
            d = {"page_size": page_size}
            if filter_obj:
                d["filter"] = filter_obj
            if pt:
                d["page_token"] = pt
            res = self._call(url, d, "POST")
            out += res.get("items", [])
            pt = res.get("page_token")
            if not res.get("has_more"):
                break
        return out

    def get(self, table_key, record_id):
        url = f"/bitable/v1/apps/{self.app_token}/tables/{self.tables[table_key]}/records/{record_id}"
        return self._call(url)["record"]

    # ---------------- 写 ----------------
    def update(self, table_key, record_id, fields):
        url = f"/bitable/v1/apps/{self.app_token}/tables/{self.tables[table_key]}/records/{record_id}"
        return self._call(url, {"fields": fields}, "PUT")

    def create(self, table_key, fields):
        url = f"/bitable/v1/apps/{self.app_token}/tables/{self.tables[table_key]}/records"
        return self._call(url, {"fields": fields}, "POST")


# ---------------- 工具 ----------------
def txt(v):
    """飞书富文本字段 -> 纯字符串"""
    if isinstance(v, list):
        return "".join(x.get("text", "") for x in v if isinstance(x, dict)).strip()
    return v if isinstance(v, str) else ""


def ms2d(v):
    return datetime.datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d") if v else ""


def d2ms(d):
    return int(datetime.datetime.strptime(d, "%Y-%m-%d").timestamp() * 1000)


def link_ids(v):
    """双向关联字段 -> record_id 列表。写入时用字符串数组 ["recX"]，对象形式会报 1254074。"""
    if isinstance(v, dict):
        return v.get("link_record_ids") or []
    if isinstance(v, list):
        ids = []
        for x in v:
            if isinstance(x, dict):
                ids += x.get("link_record_ids") or []
            elif isinstance(x, str):
                ids.append(x)
        return ids
    return []
