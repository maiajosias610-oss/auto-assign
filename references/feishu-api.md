# 飞书接入细节与教训

## 认证

```
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
{"app_id": "...", "app_secret": "..."}
-> tenant_access_token（约 2 小时）
```

后续请求头 `Authorization: Bearer <token>`。用 `adapters/feishu_adapter.py` 的 `Feishu` 类即可，内部自动续期。

## 多维表格

| 用途 | 方法 |
|---|---|
| 列记录 | `POST /bitable/v1/apps/{app_token}/tables/{table_id}/records/search` |
| 读单条 | `GET  /bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}` |
| 改单条 | `PUT  /bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}` body `{"fields":{...}}` |
| 新建 | `POST /bitable/v1/apps/{app_token}/tables/{table_id}/records` body `{"fields":{...}}` |

`app_token` 在多维表格 URL 里：`https://xxx.feishu.cn/base/<app_token>`。
`table_id` 在 URL 的 `?table=<table_id>`。

## 坑位清单

1. **双向关联写入必须是字符串数组**
   正确：`"设计师": ["recXXXX"]`
   错误：`"设计师": [{"record_id": "recXXXX"}]` → 报 **1254074**

2. **直连 body 就是 `{"fields": {...}}`**
   不要套 lark-mcp 的包装格式。

3. **大表必须服务端 filter**
   任务表 13 万条时：`total` 不可信、全量拉取超时。
   ```json
   {"conjunction":"and","conditions":[
     {"field_name":"状态","operator":"isNot","value":["已完成"]},
     {"field_name":"截止时间","operator":"isNotEmpty","value":[]}
   ]}
   ```
   曾报 **1254018 InvalidFilter**——按字段名构造、避免空 value 数组以外的写法。

4. **富文本字段读出来是数组**
   `[{"text":"xxx","type":"text"}]`，取 `.text` 拼接。

5. **日期字段是毫秒时间戳**
   写入 `int(datetime.strptime(d,"%Y-%m-%d").timestamp()*1000)`。

6. **改记录别删重建**
   用 `update` 精准改需要的字段，保留其余字段与关联。

## 错误码速查

| 码 | 含义 | 处理 |
|---|---|---|
| 1254018 | InvalidFilter | 检查 filter 构造，value 用字符串数组 |
| 1254074 | 关联字段格式错 | 改成字符串数组 |
| 400006 | 腾讯文档鉴权失败 | token 过期，重新取码 |
| 99991672 | 频率限制 | 加退避重试 |
