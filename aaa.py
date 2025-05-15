from src.LitPyWeb import LitPyWeb, run

app = LitPyWeb()

@app.route('/')
def home():
    return "Welcome to the LitPyWeb RESTful API!"

run(app, host='localhost', port=8080)










