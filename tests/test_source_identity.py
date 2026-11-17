"""来源 URL 身份的固定双端向量；测试无需 Node 或网络。"""
import json
import unittest

from ai_brief.source_identity import source_identity, UnsupportedURL

CASES = json.loads(r'''[
  {
    "raw": "https://example.com/path?caret=^&a=1",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/path?a=1&caret=%5E"
      },
      "hash": "8e88704f902cf2b44973599893f7073e57e77300b6c04a54fb408f79ce42816f"
    }
  },
  {
    "raw": "https://[::ffff:c000:201]/",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://[::ffff:c000:201]/"
      },
      "hash": "ca278887e50fe407f4723dfac5b8d92f395ccea1a166638ec03916a1b9d6580f"
    }
  },
  {
    "raw": "https://[::]/",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://[::]/"
      },
      "hash": "74b04d7e03394f9db9ab90b63d25ccdb9ade75d2b99b6736b74431ae4c7e1e0e"
    }
  },
  {
    "raw": "https://[1:0:0:2:0:0:3:4]/",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://[1::2:0:0:3:4]/"
      },
      "hash": "b21e98b96650dcedc41fdfaae34c9392aadea8df103c111019dad08bc30b9479"
    }
  },
  {
    "raw": "https://EXAMPLE.COM",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/"
      },
      "hash": "011897270c3b74412c32c8658799da2fe64e95cb5635da5d92c4e9751f7dc10a"
    }
  },
  {
    "raw": "HTTP://Example.COM:80",
    "expected": {
      "value": {
        "method": "GET",
        "url": "http://example.com/"
      },
      "hash": "7fc29be808eb40338c717bc45fa612bc0f707f90be6994a30aceded170d370ad"
    }
  },
  {
    "raw": "https://example.com:443/?b=2&a=1",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/?a=1&b=2"
      },
      "hash": "d0a6b5d9d82fedb7539c128d88225775f3ac132e00d6a39e1a548f7392a938d1"
    }
  },
  {
    "raw": "https://qwen.ai/research?z=hello%20world&a=~&a=x+y",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://qwen.ai/research?a=%7E&a=x+y&z=hello+world"
      },
      "hash": "70e26a9f1e0bb9219b74c3a2d2bdcbcb5767d9eeae47bd41752588bd7242697a"
    }
  },
  {
    "raw": "https://api.github.com/search/repositories?per_page=20&q=fp8+GPU&sort=updated",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://api.github.com/search/repositories?per_page=20&q=fp8+GPU&sort=updated"
      },
      "hash": "2b961655cc568f79f53e1bf1e45e270f7b5ce48cedd10b8ccb85f9319baa7d11"
    }
  },
  {
    "raw": "https://example.com/a/../b/./c",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/b/c"
      },
      "hash": "50c2ef5c0fa67501b97a52f84834c429b2840b366c10cda19c9499ef41b3c685"
    }
  },
  {
    "raw": "https://example.com/%2e/%2E%2e/a",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/a"
      },
      "hash": "8d91613266b27d2f4a8842a1b0d07218d92f9fe3ebbb7001f5ed8b1790b7e058"
    }
  },
  {
    "raw": "https://example.com/a//../b",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/a/b"
      },
      "hash": "5c687e16cd2ad3ad2c80e9390dc610dbc1bdf5a908727df6136c4ebd34e4c5e4"
    }
  },
  {
    "raw": "https://example.com/a/.",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/a/"
      },
      "hash": "7588b4f3f6a2936211fd789f96175483bba451c52a47c7f5f33317b573ce6d2e"
    }
  },
  {
    "raw": "https://example.com/a/..",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/"
      },
      "hash": "011897270c3b74412c32c8658799da2fe64e95cb5635da5d92c4e9751f7dc10a"
    }
  },
  {
    "raw": "https://example.com/a//b///c/../../d",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/a//b//d"
      },
      "hash": "71fdd746a9c5520d3ca5589a4db628358f073fabc7bfdae114f63da8239905d8"
    }
  },
  {
    "raw": "https://example.com/\u4e2d\u6587/\u6a21\u578b \u53d1\u5e03?\u6a21\u578b=\u65b0 \u95fb&b=%7e&a=%FF",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/%E4%B8%AD%E6%96%87/%E6%A8%A1%E5%9E%8B%20%E5%8F%91%E5%B8%83?a=%EF%BF%BD&b=%7E&%E6%A8%A1%E5%9E%8B=%E6%96%B0+%E9%97%BB"
      },
      "hash": "c94b1723d152c73d75d5b9c2a3b0838a46040efa9a622e2e73371eeb9d7e5bc9"
    }
  },
  {
    "raw": "https://example.com/a%2Fb/%41/%zz?b=%zz&a=%E4%B8%AD",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/a%2Fb/%41/%zz?a=%E4%B8%AD&b=%25zz"
      },
      "hash": "1a3143e7f7d9e4df361a978d56955d0d4a187ea032bee23ba85e77f22572494c"
    }
  },
  {
    "raw": "https://example.com/a[0]|x%5Ey;z@k:foo?x=!*'()~",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/a[0]|x%5Ey;z@k:foo?x=%21*%27%28%29%7E"
      },
      "hash": "daeca16700d5f8276ef381cf6493bc42b8049da803b293efb24619e5c67d1df9"
    }
  },
  {
    "raw": "https://example.com/?x=1&&empty&=z&x=2&b=%00",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/?=z&b=%00&empty=&x=1&x=2"
      },
      "hash": "d2566f2b61df99349ed91444a12d88e483f48186384e6c715c73abb3a6ab95c4"
    }
  },
  {
    "raw": "https://example.com/?\ud800\udc00=x&\ue000=y&x=1&x=2",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/?x=1&x=2&%F0%90%80%80=x&%EE%80%80=y"
      },
      "hash": "161f1f845ec6e9d9f4d8f1ab4d345520c7cb09298925ae49651bcd1c3df1acbd"
    }
  },
  {
    "raw": "https://example.com/?",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/"
      },
      "hash": "011897270c3b74412c32c8658799da2fe64e95cb5635da5d92c4e9751f7dc10a"
    }
  },
  {
    "raw": "https://example.com/?&&",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/"
      },
      "hash": "011897270c3b74412c32c8658799da2fe64e95cb5635da5d92c4e9751f7dc10a"
    }
  },
  {
    "raw": "https://example.com:0443/path",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/path"
      },
      "hash": "8527c90e1ab298708057956e72260eb6aabf3c770d40dcabcc4037b199725537"
    }
  },
  {
    "raw": "http://[2001:0DB8::1]:80/a",
    "expected": {
      "value": {
        "method": "GET",
        "url": "http://[2001:db8::1]/a"
      },
      "hash": "eb6d9c4bcabb2bc1eb530833c7f359d54488dfc18fe361d536e962072247bc51"
    }
  },
  {
    "raw": "https://example.com/a?b=%F0%80%80%80&a=2",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/a?a=2&b=%EF%BF%BD%EF%BF%BD%EF%BF%BD%EF%BF%BD"
      },
      "hash": "f1865ca1d7c3b4b68af29183ff13ee2b0c34e87821a9e3ec26a71de387f1d076"
    }
  },
  {
    "raw": "https://example.com/a`b{c}d<e>f\"g",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/a%60b%7Bc%7Dd%3Ce%3Ef%22g"
      },
      "hash": "957490c3ae600d4fb1db0a04a5fa7849330e8702167a01976b6f27a93797e3f3"
    }
  },
  {
    "raw": "https://example.com/a/%2e./b",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/b"
      },
      "hash": "c33544f121b8820bdde42bcabc33c8c88b956232f2356c03ba427347b3c39106"
    }
  },
  {
    "raw": "https://example.com/a/.%2E/b",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/b"
      },
      "hash": "c33544f121b8820bdde42bcabc33c8c88b956232f2356c03ba427347b3c39106"
    }
  },
  {
    "raw": "https://example.com/a/%252e/b",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/a/%252e/b"
      },
      "hash": "fa0238653d87ab19cd712fd28d07a6fdfcefb2c8a9ccc6ee0caa3f44a7b0f745"
    }
  },
  {
    "raw": "https://example.com/?a=%ED%A0%80&b=+%2B",
    "expected": {
      "value": {
        "method": "GET",
        "url": "https://example.com/?a=%EF%BF%BD%EF%BF%BD%EF%BF%BD&b=+%2B"
      },
      "hash": "989b3b67193039f7cce0ef87e7df3956ce8510a8595b300c1cc3180be9727352"
    }
  }
]''')
UNSUPPORTED = json.loads(r'''[
  "https://[v1.example]/",
  "https://[vF.a:b]/",
  "https://[example.com]/",
  "https://[::1]suffix/",
  "https://[::1].example/",
  "https://[[::1]]/",
  "https://[::1]:443suffix/",
  "https://[::1]:/",
  "https://[::1%25eth0]/",
  "https://[::1]:443:5/",
  "https://::1/",
  "https://[::1]extra:443/",
  "https://example.com/a^b",
  "https://0x/",
  "https://xn--/a",
  "https://xn--a.com/a",
  "https://xn--bcher-kva.de/",
  "https://user@example.com",
  "https://example.com/#x",
  "https://example.com/#",
  "https://\u4f8b\u5b50.\u4e2d\u56fd/a",
  "http://127.1/",
  "http://0x7f000001/",
  "https://exa%6dple.com/",
  "https://example.com/a\\b",
  "https://example.com:\t443/a",
  "https://example.com:",
  " https://example.com/"
]''')


class SourceIdentityTests(unittest.TestCase):
    def test_node_whatwg_identity_vectors(self):
        for case in CASES:
            with self.subTest(url=case["raw"]):
                self.assertEqual(source_identity(case["raw"]), case["expected"])

    def test_unsupported_forms_fail_closed(self):
        for raw in UNSUPPORTED:
            with self.subTest(url=raw):
                with self.assertRaises(UnsupportedURL):
                    source_identity(raw)

    def test_invalid_types_and_length_are_rejected(self):
        for raw in (None, 1, "", "https://example.com/" + "a" * 2000):
            with self.subTest(url=raw):
                with self.assertRaises(UnsupportedURL):
                    source_identity(raw)
