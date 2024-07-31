# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os
import logging  # Import the logging module for logging purposes
import streamlit as st  # Import the Streamlit library for building interactive web apps
import chatbot_lib as glib  # Import a custom module named 'chatbot_lib'
from dotenv import load_dotenv  # Import the load_dotenv function from the 'dotenv' module

# Set up the logging configuration with the INFO level and a specific format
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# Load environment variables from a '.env' file
load_dotenv()

st.set_page_config(page_title="Chatbot")  # Set the page title for the Streamlit app
st.title("Chatbot")  # Display the title "Chatbot" on the Streamlit app

username = st.text_input("Enter your username:")  # Get the username from the user input
session_id = username  # Set the session_id to the username
redis_url = os.environ.get("ELASTICACHE_ENDPOINT_URL")  # Get the Redis URL from the environment variables
key_prefix = "chat_history:"  # Set a prefix for the chat history key

if username:
    st.session_state.username = username  # Store the username in the session state
    st.info(f"Logged in as: {username}")  # Display a message with the logged-in username

# If 'memory' is not in the session state, initialize it by calling the 'get_memory' function from 'chatbot_lib'
if 'memory' not in st.session_state:
    st.session_state.memory = glib.get_memory(session_id=session_id, url=redis_url, key_prefix=key_prefix)

# If 'chat_history' is not in the session state, initialize it as an empty list
if 'chat_history' not in st.session_state:
    st.session_state.chat_history = []

# Update the 'memory' in the session state by calling the 'get_memory' function from 'chatbot_lib'
st.session_state.memory = glib.get_memory(session_id=session_id, url=redis_url, key_prefix=key_prefix)

# Display the chat history by iterating over the messages and rendering them using Streamlit's chat_message
for message in st.session_state.chat_history:
    with st.chat_message(message["role"]):
        st.markdown(message["text"])

input_text = st.chat_input("Chat with your bot here")  # Get the user's input text from the chat input

if input_text:
    with st.chat_message("user"):
        st.markdown(input_text)  # Display the user's input text in the chat
    logging.info(f"Input text to the model: {input_text}")  # Log the user's input text
    st.session_state.chat_history.append({"role": "user", "text": input_text})  # Add the user's input to the chat history
    logging.info(f"Memory to the Model: {st.session_state.memory}")  # Log the memory passed to the model
    chat_response = glib.get_chat_response(input_text=input_text, memory=st.session_state.memory)  # Get the chat response from the 'chatbot_lib' module
    with st.chat_message("assistant"):
        st.markdown(chat_response)  # Display the chat response in the chat
    st.session_state.chat_history.append({"role": "assistant", "text": chat_response})  # Add the chat response to the chat history
