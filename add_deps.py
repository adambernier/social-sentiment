import glob

for f in glob.glob('*/requirements.in'):
    if 'ui-service' in f or 'grafana' in f:
        continue
    with open(f, 'r') as file:
        content = file.read()
    if 'prometheus_client' not in content:
        with open(f, 'a') as file:
            file.write('\nprometheus_client\n')
            if 'api-service' in f:
                file.write('prometheus-fastapi-instrumentator\n')
