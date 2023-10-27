# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


import os
import streamlit as st #all streamlit commands will be available through the "st" alias
import chatbot_lib as glib #reference to local lib script


st.set_page_config(page_title="Chatbot") #HTML title
st.title("Chatbot") #page title
# Add a login mechanism for the user

username = st.text_input("Enter your username:")
session_id = username
print("username being sent to session", session_id)
redis_url=os.environ.get("ELASTICACHE_ENDPOINT_URL")
key_prefix="chat_history:"

if username:
    st.session_state.username = username  # Store the username in the session state
    st.info(f"Logged in as: {username}")

if 'memory' not in st.session_state: #see if the memory hasn't been created yet
    print(f"DEBUG: session_id: {session_id}")
    st.session_state.memory = glib.get_memory(session_id=session_id, url=redis_url, key_prefix=key_prefix) #initialize the memory

if 'chat_history' not in st.session_state: #see if the chat history hasn't been created yet
    st.session_state.chat_history = [] #initialize the chat history

print(f"DEBUG: session_id2: {session_id}")
st.session_state.memory = glib.get_memory(session_id=session_id, url=redis_url, key_prefix=key_prefix) #initialize the memory

#Re-render the chat history (Streamlit re-runs this script, so need this to preserve previous chat messages)
for message in st.session_state.chat_history: #loop through the chat history
    with st.chat_message(message["role"]): #renders a chat line for the given role, containing everything in the with block
        st.markdown(message["text"]) #display the chat content

input_text = st.chat_input("Chat with your bot here") #display a chat input box

if input_text: #run the code in this if block after the user submits a chat message
    with st.chat_message("user"): #display a user chat message
        st.markdown(input_text) #renders the user's latest message
    #Debugging : Pring the input_text to the model 
    print("Input text to the model:", input_text)
    st.session_state.chat_history.append({"role":"user", "text":input_text}) #append the user's latest message to the chat history
    #Debugging: Print the memory being sent to the model 
    print("Memory to the Model:", st.session_state.memory)
    chat_response = glib.get_chat_response(input_text=input_text, memory=st.session_state.memory) #call the model through the supporting library
    with st.chat_message("assistant"): #display a bot chat message
        st.markdown(chat_response) #display bot's latest response
    st.session_state.chat_history.append({"role":"assistant", "text":chat_response}) #append the bot's latest message to the chat history
