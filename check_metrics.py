import urllib.request
try:
    with urllib.request.urlopen('http://localhost:8001/') as response:
        html = response.read()
        with open('metrics_output.txt', 'wb') as f:
            f.write(html)
except Exception as e:
    with open('metrics_output.txt', 'w') as f:
        f.write(str(e))
