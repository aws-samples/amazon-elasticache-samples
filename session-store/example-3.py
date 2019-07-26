import os

import redis
from flask import Flask, session, redirect, escape, request


# Configure the application name with the FLASK_APP environment variable.
app = Flask(__name__)


# Configure the secret_key with the SECRET_KEY environment variable.
app.secret_key = os.environ.get('SECRET_KEY', default=None)


# Connect to Redis with the REDIS_URL environment variable.
store = redis.Redis.from_url(os.environ.get('REDIS_URL'))


@app.route('/')
def index():
    if 'username' in session:
        username = escape(session['username'])

        visits = store.hincrby(username, 'visits', 1)

        store.expire(username, 10)

        return '''
            Logged in as {0}.<br>
            Visits: {1}
        '''.format(username, visits)

    return 'You are not logged in'


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        session['username'] = request.form['username']
        return redirect('/')
    return '''
        <form method="post">
        <p><input type=text name=username>
        <p><input type=submit value=Login>
        </form>
    '''


@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect('/')
