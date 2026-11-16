# ECR Public mirror of the Docker official image (not Docker Hub).
FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
# The app binds loopback by default. Inside the container it has to accept the
# published port; who can reach that port is decided by the publish (127.0.0.1
# in docker-compose.yml, loopback on the instance in AccessMode=ssm) and the
# instance security group.
ENV ECDL_BIND_HOST=0.0.0.0

CMD ["python", "app.py"]
