import os
os.environ['HTTP_PROXY'] = 'http://127.0.0.1:7897'
os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:7897'
os.environ['ALL_PROXY'] = 'http://127.0.0.1:7897'

# 强制requests库使用代理
import urllib.request
proxy = urllib.request.ProxyHandler({
    'http': 'http://127.0.0.1:7897',
    'https': 'http://127.0.0.1:7897'
})
opener = urllib.request.build_opener(proxy)
urllib.request.install_opener(opener)

# 运行主程序
exec(open('main.py').read())
