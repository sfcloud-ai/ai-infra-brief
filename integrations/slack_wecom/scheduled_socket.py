"""受限本机协议处理器，部署时须单独启用。

原只读校验套接字保持不变；导入模块不会创建监听器或启动服务。"""
import base64
import json

if __package__:
    from .notice_socket import peer_uid, bounded_read
else:
    from notice_socket import peer_uid, bounded_read

MAX_REQUEST = 12*1024*1024


def dispatch(data, queue, can_admit=lambda: True):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_REQUEST:
        raise ValueError("Bounded request required")
    def unique(pairs):
        value = {}
        for key,item in pairs:
            if key in value:raise ValueError("Duplicate request field")
            value[key]=item
        return value
    request=json.loads(data.decode("utf-8"),object_pairs_hook=unique)
    if not isinstance(request,dict):raise ValueError("Exact request object required")
    if set(request)=={"action","request_id"} and request["action"]=="status":
        if not isinstance(request["request_id"],str) or len(request["request_id"])>80:
            raise ValueError("Bounded original request ID required")
        return queue.status(request["request_id"])
    if set(request)!={"action","request_id","bundle_base64","report_commit"} or request["action"]!="admit":
        raise ValueError("Unsupported operation")
    if not can_admit():
        raise ValueError("Scheduled admission paused")
    encoded=request["bundle_base64"]
    if not isinstance(encoded,str):raise ValueError("Bundle encoding required")
    bundle=base64.b64decode(encoded,validate=True)
    if base64.b64encode(bundle).decode("ascii")!=encoded:raise ValueError("Canonical base64 required")
    return queue.admit(request["request_id"],bundle,request["report_commit"])


def handle(connection, allowed_uid, queue, can_admit=lambda: True):
    if type(allowed_uid) is not int or allowed_uid<=0 or peer_uid(connection)!=allowed_uid:
        return  # 在读取请求字节之前拒绝错误调用者。
    try:
        result=dispatch(bounded_read(connection,MAX_REQUEST,10),queue,can_admit)
        encoded=json.dumps(result,ensure_ascii=True).encode("ascii")
        if len(encoded)>16384:raise ValueError("Bounded result required")
    except Exception:
        encoded=b'{"status":"not_verified","error":"scheduled_request_rejected"}'
    connection.settimeout(5)
    connection.sendall(encoded)
