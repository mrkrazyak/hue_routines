FROM python:3.12

COPY hue_routines_main.py ./
COPY hue_config.py ./
COPY requirements.txt ./

RUN pip install -r requirements.txt