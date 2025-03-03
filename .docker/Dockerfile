FROM python:3.9-bullseye
ENV DIR_BE /opt/web-backend
ENV TZ="Europe/Prague"

RUN mkdir $DIR_BE
WORKDIR $DIR_BE
EXPOSE 3032

# install and setup required software
RUN wget -O - https://deb.adamhlavacek.com/pub.gpg | apt-key add - \
    && echo "deb https://deb.adamhlavacek.com ./" >> /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y pandoc acl sudo isolate bindfs libpython3.7 libcap2 tzdata \
    && sudo apt-get clean \
    && pip3 install virtualenv --no-cache-dir \
    && virtualenv -p python3 ksi-py3-venv \
    && mkdir /opt/etc \
    && echo 'SQL_ALCHEMY_URI = "sqlite:////var/ksi-be/db.sqlite"' > config.py

ADD requirements.txt $DIR_BE/requirements.txt
RUN bash -c 'cd $DIR_BE && source ksi-py3-venv/bin/activate && pip install --no-cache-dir -r requirements.txt'

ADD . $DIR_BE/

RUN bash -c 'cd $DIR_BE  && ./init-makedirs.sh' \
    && bash -c 'cd $DIR_BE  && chmod +x .docker/entrypoint.sh' \
    && mkdir /var/ksi-be.ro \
    && mkdir /var/ksi-be \
    && mkdir /var/ksi-seminar \
    && mkdir /var/ksi-module-lib \
    && mkdir "$DIR_BE/data/module_lib" \
    && chmod 755 "$DIR_BE/data/module_lib" \
    && mv "$DIR_BE/.docker/config_isolate" /usr/local/etc/isolate \
    && chmod 644 /usr/local/etc/isolate \
    && chmod u+s /usr/bin/isolate \
    && chown root:root /usr/local/etc/isolate \
    && sed -e 's/DISABLE_CORS = False/DISABLE_CORS = True/' -i app.py \
    && sed -e 's/READ COMMITTED",/SERIALIZABLE", connect_args={"check_same_thread": False},/' -i db.py \
    && sed -e 's/127.0.0.1/0.0.0.0/' -i gunicorn_cfg.py \
    && sed -e 's/CURRENT_TIMESTAMP + INTERVAL 1 SECOND/datetime("now", "+1 seconds")/' -i endpoint/post.py

RUN useradd -Mr ksi

ENTRYPOINT ["/bin/bash"]
CMD ["./.docker/entrypoint.sh"]
