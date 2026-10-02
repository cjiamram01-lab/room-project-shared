from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI

# Support launching through this legacy entry point as well.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from routes.api import router as api_router

app = FastAPI()

origins = ["http://localhost:8030","http://0.0.0.0:8030"]

# CLIENT_ID = os.getenv('25133f28f9e842998a662eb4b935cf2a')
# TENANT_ID = os.getenv('8b060fcf-29fd-4b25-a613-c4712345cfd9')
# AUTHORITY = f'https://login.microsoftonline.com/{TENANT_ID}'
# SCOPE = ['User.Read']

app.add_middleware(
     CORSMiddleware,
     allow_origins=["*"],
     allow_credentials=True,
     allow_methods=["*"],  # HTTP methods to allow
     allow_headers=["*"]
)

app.include_router(api_router)
if __name__ == '__main__':
    uvicorn.run("main:app", host='0.0.0.0', port=8000, log_level="info",reload=True)#Developer code
    # command: uvicorn app.main:app --host 0.0.0.0
    #**********Production********************************
    #uvicorn.run(app, host="0.0.0.0", port=8000)  make for production code
    #bash uvicorn main:app --reload
    # Security SSL isAuthen
    #uvicorn.run("main:app", host='0.0.0.0', port=8030, log_level="info", ssl_keyfile="/etc/apache2/ssl/apache.key", ssl_certfile="/etc/apache2/ssl/apache.crt",   reload=False)
    print("running")
