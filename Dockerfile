# build:
# docker build -t aecid/aag .
#
# Run the container and pass the SERVERURL as an environment-variable:
# docker run -it --rm -e ELASTIC_SERVER=http://172.17.0.2:9000 -e ELASTIC_INDEX=aminer-anomalies aecid/aag

FROM python:latest
LABEL maintainer="wolfgang.hotwagner@ait.ac.at"

ARG UNAME=aag
ARG UID=1000
ARG GID=1000

# Install necessary debian packages
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
        sudo

RUN groupadd -g $GID -o $UNAME && useradd -u $UID -g $GID -ms /usr/sbin/nologin $UNAME

ADD . /app

RUN chown $UID.$GID -R /app && cd /app && sudo -u $UNAME pip3 install -r requirements.txt

USER $UNAME
WORKDIR /app

CMD ["python3","generator.py"]
