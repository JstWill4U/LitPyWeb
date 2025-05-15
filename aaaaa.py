from bottle import Bottle, run

app = Bottle()

@app.route('/')
def home():
    return "Welcome to the Bottle RESTful API!"

run(app, host='localhost', port=8080)
