from setuptools import setup

setup(
    name="millheater",
    packages=["mill"],
    install_requires=["aiohttp>=3.7.4,<4", "async_timeout>=3.0.0"],
    version="0.11.0",
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
