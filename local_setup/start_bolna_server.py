import uvicorn

if __name__ == "__main__":
    uvicorn.run("quickstart_server:app", host="0.0.0.0", port=5001, reload=True)