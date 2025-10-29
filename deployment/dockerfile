# pthon version
FROM python:3.12

# the directory where the code will be stored inside docker container
WORKDIR /code

COPY ./requirements.txt /code/requirements.txt
COPY ./setup.py /code/setup.py
COPY ./README.md /code/README.md
COPY ./app /code/app

RUN pip install --no-cache-dir --upgrade .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

EXPOSE 8001

# run app in prod
CMD ["python", "app/main.py", "--docker"]