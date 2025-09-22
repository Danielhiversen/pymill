from setuptools import setup

version_ns = {}
with open("mill/consts.py", "r", encoding="utf-8") as f:
    exec(f.read(), version_ns)
__version__ = version_ns["__version__"]

setup(
    name="millheater",
    packages=["mill"],
    install_requires=["aiohttp>=3.7.4,<4", "PyJWT>=2"],
    version=__version__,
    description="A python3 library to communicate with Mill",
    long_description="A python3 library to communicate with Mill",
    python_requires=">=3.10",
    author="Daniel Hjelseth Hoyer",
    author_email="mail@dahoiv.net",
    url="https://github.com/Danielhiversen/pymill",
    license="MIT",
    classifiers=[
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Topic :: Home Automation",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
