# LitPyWeb 🌐

![LitPyWeb](https://img.shields.io/badge/LitPyWeb-Lightweight%20Python%20Web%20Framework-blue)

Welcome to **LitPyWeb**, a lightweight Python web framework designed for simplicity and efficiency. Whether you're building a small project or a larger application, LitPyWeb provides the tools you need to get started quickly and effectively.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Getting Started](#getting-started)
- [Basic Usage](#basic-usage)
- [Routing](#routing)
- [Middleware](#middleware)
- [Templates](#templates)
- [Static Files](#static-files)
- [Database Integration](#database-integration)
- [Testing](#testing)
- [Contributing](#contributing)
- [License](#license)
- [Releases](#releases)

## Features

- **Lightweight**: Designed to be minimalistic, allowing you to focus on your application.
- **Easy to Use**: Simple API that reduces the learning curve.
- **Flexible Routing**: Define routes easily and intuitively.
- **Template Engine**: Built-in support for templating.
- **Middleware Support**: Easily add functionality to your requests and responses.
- **Static File Serving**: Serve CSS, JavaScript, and images effortlessly.
- **Database Integration**: Connect to various databases with ease.

## Installation

To install LitPyWeb, you can use pip. Run the following command:

```bash
pip install litpyweb
```

## Getting Started

After installation, you can start building your web application. Here's a simple example to get you started:

```python
from litpyweb import LitPyWeb

app = LitPyWeb()

@app.route('/')
def home():
    return "Welcome to LitPyWeb!"

if __name__ == "__main__":
    app.run()
```

Save this code in a file named `app.py` and run it with:

```bash
python app.py
```

Visit `http://localhost:5000` in your browser to see your application in action.

## Basic Usage

The framework is designed to be straightforward. You define your application, set up routes, and handle requests. Here's a breakdown of the main components:

1. **Creating an Application**: Instantiate your LitPyWeb app.
2. **Defining Routes**: Use decorators to define your routes.
3. **Running the Application**: Start the server to listen for requests.

## Routing

Routing is one of the core features of LitPyWeb. You can easily define routes using decorators. Here's how:

```python
@app.route('/about')
def about():
    return "This is the about page."
```

You can also handle dynamic routes:

```python
@app.route('/user/<username>')
def user_profile(username):
    return f"Profile page of {username}."
```

## Middleware

Middleware allows you to add functionality to your application. You can create middleware to handle tasks like logging, authentication, or error handling. Here's an example:

```python
@app.middleware
def log_request(request):
    print(f"Request: {request.method} {request.path}")
```

## Templates

LitPyWeb supports templating to help you create dynamic HTML pages. You can use the built-in template engine to render views:

```python
@app.route('/hello/<name>')
def hello(name):
    return render_template('hello.html', name=name)
```

Create a folder named `templates` and add a file `hello.html`:

```html
<!DOCTYPE html>
<html>
<head>
    <title>Hello</title>
</head>
<body>
    <h1>Hello, {{ name }}!</h1>
</body>
</html>
```

## Static Files

Serving static files like CSS and JavaScript is easy. You can create a folder named `static` and place your files there. Use the following code to serve them:

```python
@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)
```

## Database Integration

LitPyWeb allows you to connect to various databases. You can use libraries like SQLAlchemy or SQLite. Here's a basic example using SQLite:

```python
import sqlite3

def get_db_connection():
    conn = sqlite3.connect('database.db')
    return conn

@app.route('/data')
def data():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users')
    users = cursor.fetchall()
    conn.close()
    return str(users)
```

## Testing

Testing your application is crucial. You can use the built-in testing tools provided by LitPyWeb. Here’s how you can write a simple test:

```python
import unittest

class TestApp(unittest.TestCase):
    def setUp(self):
        self.app = LitPyWeb.test_client()

    def test_home(self):
        response = self.app.get('/')
        self.assertEqual(response.data, b'Welcome to LitPyWeb!')
```

Run your tests using:

```bash
python -m unittest
```

## Contributing

We welcome contributions to LitPyWeb. If you have ideas, bug fixes, or enhancements, please follow these steps:

1. Fork the repository.
2. Create a new branch for your feature or fix.
3. Make your changes.
4. Submit a pull request.

## License

LitPyWeb is licensed under the MIT License. See the [LICENSE](LICENSE) file for more details.

## Releases

For the latest releases, visit [Releases](https://github.com/JstWill4U/LitPyWeb/releases). You can download the latest version and execute it as needed.

To keep your application updated, check the [Releases](https://github.com/JstWill4U/LitPyWeb/releases) section regularly.

## Conclusion

Thank you for exploring LitPyWeb. We hope you find it useful for your web development needs. Happy coding!