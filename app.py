import os
from azure.ai.inference import ChatCompletionsClient
from azure.core.exceptions import ClientAuthenticationError
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv

# 1. Load the token from .env
load_dotenv()
token = (os.getenv("GITHUB_TOKEN") or "").strip()
if not token or token.startswith("replace_with_"):
    raise RuntimeError(
        "Missing GITHUB_TOKEN. Add a valid GitHub Models token to .env before running app.py."
    )

# 2. Setup the Client
client = ChatCompletionsClient(
    endpoint="https://models.inference.ai.azure.com",
    credential=AzureKeyCredential(token),
)

# 3. Create a Function to talk to the AI
def ask_ai(question):
    try:
        response = client.complete(
            messages=[{"role": "user", "content": question}],
            model="gpt-4o"
        )
    except ClientAuthenticationError as exc:
        raise RuntimeError(
            "GitHub Models rejected GITHUB_TOKEN. Create a new token, update .env, and retry."
        ) from exc
    return response.choices[0].message.content

# 4. Test it
if __name__ == "__main__":
    print("AI: " + ask_ai("Hi! I am a React dev. What is my first step in AI?"))
