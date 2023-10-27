import os
from langchain.memory import ConversationSummaryBufferMemory, RedisChatMessageHistory
from langchain.llms.bedrock import Bedrock
from langchain.chains import ConversationChain
from langchain.prompts.prompt import PromptTemplate


redis_url=os.environ.get("ELASTICACHE_ENDPOINT_URL")


def get_llm():
    model_kwargs =  { 
        "max_tokens_to_sample": 8000,
            "temperature": 0, 
            "top_k": 50, 
            "top_p": 1, 
            "stop_sequences": ["\n\nHuman:"] 
    }
    # Amazon Bedrock endpoints and quotas => https://docs.aws.amazon.com/general/latest/gr/bedrock.html
    llm = Bedrock(
        credentials_profile_name=os.environ.get("BWB_PROFILE_NAME"), #sets the profile name to use for AWS credentials (if not the default)
        region_name=os.environ.get("BWB_REGION_NAME"), #sets the region name (if not the default)
        endpoint_url=os.environ.get("BWB_ENDPOINT_URL"), #sets the endpoint URL (if necessary)
        model_id="anthropic.claude-v2", #use the Anthropic Claude model
        model_kwargs=model_kwargs) #configure the properties for Claude
    return llm
    
    
def get_chat_history():
    chat_history=RedisChatMessageHistory(session_id='username', url=redis_url, key_prefix="chat_history:") 
    return chat_history


def get_memory(session_id, url, key_prefix): # create memory for this chat session
    # ConversationSummaryBufferMemory requires an LLM for summarizing older messages
    # this allows us to maintain the "big picture" of a long-running conversation
    llm = get_llm()
    chat_history=RedisChatMessageHistory(session_id=session_id, url=url, key_prefix=key_prefix) 
    memory = ConversationSummaryBufferMemory(ai_prefix="AI Assistant",llm=llm, max_token_limit=1024, chat_memory=chat_history) #Maintains a summary of previous messages
    # memory = ConversationSummaryBufferMemory(llm=llm, max_token_limit=1024) # Maintains a summary of previous messages
    return memory


def get_chat_response(input_text, memory): #chat client function
    llm = get_llm()
    template = """The following is a friendly conversation between a human and an AI. 
    AI provide very concise responses. If the AI does not know the answer to a question, it truthfully says it does not know.
    Current conversation:{history}. Human: {input}  AI Assistant:"""
    PROMPT = PromptTemplate(input_variables=["history", "input"], template=template)
    conversation_with_summary = ConversationChain( #create a chat client
        llm = llm, #using the Bedrock LLM
        memory = memory, #with the summarization memory
        prompt=PROMPT,
        verbose = True #print out some of the internal states of the chain while running
    )
    chat_response = conversation_with_summary.predict(input=input_text) #pass the user message and summary to the model
    return chat_response

