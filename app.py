import os
from azure.ai.inference import ChatCompletionsClient
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv

# 1. Load the token from .env
load_dotenv()
token = os.getenv("GITHUB_TOKEN")

# 2. Setup the Client
client = ChatCompletionsClient(
    endpoint="https://models.inference.ai.azure.com",
    credential=AzureKeyCredential(token),
)

# 3. Create a Function to talk to the AI
def ask_ai(question):
    response = client.complete(
        messages=[{"role": "user", "content": question}],
        model="gpt-4o" 
    )
    return response.choices[0].message.content

# 4. Test it
if __name__ == "__main__":
    print("AI: " + ask_ai("Hi! I am a React dev. What is my first step in AI?"))
