"""固定的持久本机验证管道，不是网络或用户服务。

仅可信部署配置可指定绝对程序路径。来源缓存留在 Node 内，不序列化，
也不能由请求注入。"""
import base64
import json
import os
from pathlib import Path
import selectors
import subprocess
import time


class PublicationRejected(ValueError):
    pass


class NodePublicationVerifier:
    def __init__(self, node, script):
        self.argv = (str(Path(node)), str(Path(script)), "--serve")
        if any(not Path(p).is_absolute() for p in self.argv[:2]):
            raise ValueError("Trusted absolute verifier paths required")
        self.process = None

    def close(self):
        if self.process is not None:
            process,self.process=self.process,None
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close();process.stdout.close()

    def __call__(self, bundle, commit):
        if not isinstance(bundle, bytes) or not 0 < len(bundle) <= 8*1024*1024:
            raise ValueError("Bounded original bundle required")
        request = {"bundle_base64":base64.b64encode(bundle).decode("ascii"),"report_commit":commit}
        if self.process is None or self.process.poll() is not None:
            self.close()
            self.process=subprocess.Popen(self.argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL,bufsize=0,env={"LANG":"C.UTF-8"})
        try:
            payload=json.dumps(request).encode()+b"\n"
            # 总期限覆盖管道写入和可能缓慢的网络读取。
            deadline=time.monotonic()+150
            output=bytearray();offset=0
            with selectors.DefaultSelector() as selector:
                os.set_blocking(self.process.stdin.fileno(),False)
                os.set_blocking(self.process.stdout.fileno(),False)
                selector.register(self.process.stdin,selectors.EVENT_WRITE)
                selector.register(self.process.stdout,selectors.EVENT_READ)
                while b"\n" not in output:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError("Publication verifier deadline")
                    events=selector.select(remaining)
                    if not events:raise TimeoutError("Publication verifier deadline")
                    for key,_ in events:
                        if key.fileobj is self.process.stdin:
                            offset+=os.write(key.fd,payload[offset:offset+65536])
                            if offset==len(payload):selector.unregister(self.process.stdin)
                        else:
                            chunk=os.read(key.fd,65536)
                            if not chunk:raise ValueError("Publication verifier exited")
                            output.extend(chunk)
                            if len(output)>3*1024*1024:raise ValueError("Bounded verification result required")
            if not output.endswith(b"\n") or output.count(b"\n")!=1:
                raise ValueError("One exact verifier response required")
            response=json.loads(output)
            if response=={"ok":False,"error":"publication_not_verified"}:
                raise PublicationRejected("Publication not verified")
            if set(response)!={"ok","result"} or response["ok"] is not True:
                raise ValueError("Publication verifier protocol differs")
            plan=response["result"]
            plan["poster"]=base64.b64decode(plan.pop("poster_base64"),validate=True)
            return plan
        except PublicationRejected:
            # 合法的否定读取结果不代表管道损坏；保留内部来源缓存，
            # 下次仍重新核验所有公开内容。
            raise
        except Exception:
            self.close()
            raise


class NodeHistoricalVerifier:
    """仅供停机迁移的来源验证器，不接入在线调度接口或实时发送。"""
    def __init__(self, node, script):
        self.argv=(str(Path(node)),str(Path(script)),"--history")
        if any(not Path(p).is_absolute() for p in self.argv[:2]):
            raise ValueError("Trusted absolute verifier paths required")

    def __call__(self,bundle,commit):
        if not isinstance(bundle,bytes) or not 0<len(bundle)<=8*1024*1024:
            raise ValueError("Bounded historical bundle required")
        request={"bundle_base64":base64.b64encode(bundle).decode("ascii"),"report_commit":commit}
        result=subprocess.run(self.argv,input=json.dumps(request).encode(),capture_output=True,
                              timeout=150,check=True,env={"LANG":"C.UTF-8"})
        if not 0<len(result.stdout)<=3*1024*1024:raise ValueError("Bounded history proof required")
        value=json.loads(result.stdout)
        if set(value)!={"purpose","delivery_allowed","plan"} or value["purpose"]!="historical_daily_claims_only" or value["delivery_allowed"] is not False:
            raise ValueError("Historical source proof required")
        plan=value["plan"];plan["poster"]=base64.b64decode(plan.pop("poster_base64"),validate=True)
        return plan
