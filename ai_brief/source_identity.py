"""常规 HTTP 来源身份的 WHATWG 兼容子集；歧义输入拒绝而非猜测。"""
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit, parse_qsl, quote

class UnsupportedURL(ValueError):
    pass

def canonical_source_url(raw):
    """仅镜像通过双端测试的子集，不是完整 WHATWG URL 解析器。"""
    if (not isinstance(raw, str) or len(raw.encode('utf-16-le', errors='surrogatepass')) // 2 > 2000
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in raw)
            or raw.startswith(' ') or raw.endswith(' ') or '\\' in raw or '#' in raw or not re.match(r'^https?://',raw,re.I)):
        raise UnsupportedURL('Unsupported HTTP URL syntax')
    try:
        parts=urlsplit(raw)
        if not parts.netloc or '@' in parts.netloc or parts.scheme.lower() not in ('http','https'):
            raise UnsupportedURL('Unsupported HTTP authority')
        if '[' in parts.netloc or ']' in parts.netloc:
            bracketed=re.fullmatch(r'\[([0-9a-fA-F:.]+)\](?::[0-9]+)?',parts.netloc)
            if bracketed is None or ':' not in bracketed.group(1):
                raise UnsupportedURL('Bracketed authority must contain only an IPv6 address and optional port')
        host=parts.hostname
        if not host or not host.isascii() or '%' in host:
            raise UnsupportedURL('Unsupported hostname encoding')
        port=parts.port
        if parts.netloc.endswith(':'):
            raise UnsupportedURL('Empty port is outside the supported subset')
        if ':' in host:
            packed=ipaddress.IPv6Address(host).packed
            groups=[int.from_bytes(packed[n:n+2],'big') for n in range(0,16,2)]
            best_start,best_length=-1,1
            index=0
            while index<8:
                if groups[index]: index+=1; continue
                end=index
                while end<8 and not groups[end]: end+=1
                if end-index>best_length: best_start,best_length=index,end-index
                index=end
            words=[format(value,'x') for value in groups]
            if best_start>=0:
                host=':'.join(words[:best_start])+'::'+':'.join(words[best_start+best_length:])
            else: host=':'.join(words)
            host='['+host+']'
        else:
            host=host.lower()
            if any(label.startswith('xn--') for label in host.split('.')):
                raise UnsupportedURL('IDNA hostnames are outside the supported subset')
            if not re.fullmatch(r'[a-z0-9_.-]+',host):
                raise UnsupportedURL('Unsupported hostname')
            last=host.rstrip('.').rsplit('.',1)[-1]
            if last.isdigit() or re.fullmatch(r'0x[0-9a-f]*',last):
                host=str(ipaddress.IPv4Address(host))
        scheme=parts.scheme.lower()
        authority=host+(':'+str(port) if port is not None and port != (443 if scheme=='https' else 80) else '')
        path=parts.path or '/'
        if '^' in path:
            raise UnsupportedURL('Raw path caret differs across supported host runtimes')
        if not path.startswith('/'):
            raise UnsupportedURL('Unsupported path')
        segments=[]
        raw_segments=path.split('/')[1:]
        for index, segment in enumerate(raw_segments):
            dot=re.sub(r'%2e','.',segment,flags=re.I)
            last_segment=index==len(raw_segments)-1
            if dot=='.':
                if last_segment: segments.append('')
            elif dot=='..':
                if segments: segments.pop()
                if last_segment: segments.append('')
            else:
                segments.append(segment)
        path='/'+'/'.join(segments)
        # 特殊 URL 的路径编码集；已有百分号序列原样保留。
        path=quote(path,safe="/!$&'()*+,-.:;=@_~%[]|^",encoding='utf-8',errors='strict')
        pairs=parse_qsl(parts.query,keep_blank_values=True,encoding='utf-8',errors='replace')
        pairs.sort(key=lambda pair:pair[0].encode('utf-16-be'))
        def form(value):
            return quote(value,safe='*-._',encoding='utf-8',errors='strict').replace('~','%7E').replace('%20','+')
        query='&'.join(form(k)+'='+form(v) for k,v in pairs)
        return scheme+'://'+authority+path+('?' + query if query else '')
    except (ValueError,UnicodeError) as exc:
        if isinstance(exc,UnsupportedURL): raise
        raise UnsupportedURL('Unsupported HTTP URL') from exc

def source_identity(raw):
    value={'method':'GET','url':canonical_source_url(raw)}
    encoded=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return {'value':value,'hash':hashlib.sha256(encoded).hexdigest()}
