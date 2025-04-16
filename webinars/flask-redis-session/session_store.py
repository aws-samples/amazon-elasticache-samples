from flask import Flask, request, session, render_template_string, redirect, url_for, make_response
import os
import redis
from redis.cluster import RedisCluster
import uuid  # Import uuid library for generating session tokens
from datetime import timedelta

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', default=None)

# Connect to the Redis server for session management
# Configure session to use Redis via RedisCluster
app.config['SESSION_TYPE'] = 'redis'
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=15)
app.config['SESSION_KEY_PREFIX'] = 'session:'
app.config['SESSION_USE_SIGNER'] = True
REDIS_URL = os.environ.get('REDIS_URL', default=None)
# Initialize the session extension
#session_redis = RedisCluster(host='REDIS_URL', port=6379, decode_responses=True, skip_full_coverage_check=True)
session_redis=RedisCluster(
           host=REDIS_URL, 
           port=6379,
           ssl=True, 
           decode_responses=True, 
           ssl_cert_reqs="none")
app.config['SESSION_REDIS'] = session_redis

redis_client = RedisCluster(host=REDIS_URL, port=6379, decode_responses=True, ssl=True, skip_full_coverage_check=True)

# Redis Set to capture expired session users
expired_users_set = "expired_users"

# Function to generate a unique session token
def generate_session_token():
    return str(uuid.uuid4())

@app.route('/')
def index():
    if 'username' in session:
        username = session['username']
        cart = get_cart(username)
        visits = increment_visits(username)  # Increment visits on each page load
        return f'Hello, {username}! Your cart: {cart}<br>Visits: {visits}'
    return 'Welcome! Please log in.'

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        if username:
            if 'username' in session:
                # User is already logged in, no need to reinitialize session data
                return redirect(url_for('index'))
            else:
                session_token = generate_session_token()
                session['username'] = username
                session['token'] = session_token  # Store the session token
                # Check if user data is already initialized in Redis
                if not user_data_exists(username):
                    init_user_data(username)  # Initialize user data (including visits) in Redis
                return redirect(url_for('index'))

    return '''
        <form method="POST">
            <label for="username">Username:</label>
            <input type="text" name="username" id="username" required>
            <input type="submit" value="Log In">
        </form>
    '''

# Add a function to check if user data exists in Redis
def user_data_exists(username):
    user_key = f'user:{username}'
    return redis_client.exists(user_key)

@app.route('/logout')
def logout():
    if 'username' in session:
        username = session.pop('username')
        delete_user_data(username) 
        redis_client.sadd(expired_users_set, username)
        session_token = session.get('token')
        if session_token:
            # Mark the session token as invalid or deleted in the server-side database
            mark_session_token_as_invalid(session_token)
        return 'Logged out.'
    return 'Logged out.'

# Function to mark a session token as invalid or deleted
def mark_session_token_as_invalid(session_token):
    invalid_tokens_set = "invalid_session_tokens"
    # Store the session token in the set of invalid tokens
    redis_client.sadd(invalid_tokens_set, session_token)
    

@app.route('/add_to_cart', methods=['GET', 'POST'])
def add_to_cart():
    if 'username' in session:
        if request.method == 'POST':
            username = session['username']
            item = request.form.get('item')
            if item:
                add_item_to_cart(username, item)
                return f'Added {item} to your cart.'
            else:
                return 'Please provide an item to add to your cart.'
        elif request.method == 'GET':
            return render_template_string('''
                <form method="POST">
                    <label for="item">Item:</label>
                    <input type="text" name="item" id="item" required>
                    <input type="submit" value="Add to Cart">
                </form>
            ''')
    return 'Please log in to add items to your cart.'

def init_user_data(username):
    user_key = f'user:{username}'
    # Initialize user data including visits count
    redis_client.hmset(user_key, {'cart_items': '', 'visits': 0})
    # Set the TTL for the user data to 15 minutes (900 seconds)
    redis_client.expire(user_key, 900)

def add_item_to_cart(username, item):
    user_key = f'user:{username}'
    # Retrieve the current cart items from Redis
    current_items = redis_client.hget(user_key, 'cart_items') or ''
    # Append the new item to the existing items
    updated_items = f'{current_items}, {item}' if current_items else item
    # Update the cart items in Redis
    redis_client.hset(user_key, 'cart_items', updated_items)

def get_cart(username):
    user_key = f'user:{username}'
    # Retrieve the cart items from Redis Hash
    return redis_client.hget(user_key, 'cart_items') or ''

def delete_user_data(username):
    user_key = f'user:{username}'
    # Delete the user's data from Redis
    redis_client.delete(user_key)

def increment_visits(username):
    user_key = f'user:{username}'
    # Increment visits count in Redis
    return redis_client.hincrby(user_key, 'visits', 1)

@app.route('/expired_users')
def display_expired_users():
    # Retrieve all usernames from the expired_users_set
    expired_usernames = redis_client.smembers(expired_users_set)
    return ', '.join(expired_usernames)

if __name__ == '__main__':
    app.run(debug=True)
