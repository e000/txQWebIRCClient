from setuptools import setup

setup(
    name = 'txQWebIRCClient',
    version = '0.2-dev',
    license='MIT',
    author='Edgeworth E. Euler',
    author_email = 'e@encyclopediadramatica.ch',
    description = 'A transport for QWebIRC',
    install_requires = [
        'twisted>=10.0.0',
    ],
    platforms = 'any',
    packages = [
        'txQWebIRCClient', 'txQWebIRCClient.examples'
    ],
    package_dir = {
        'txQWebIRCClient': 'src',
        'txQWebIRCClient.examples': 'examples'
    },
    entry_points = """
    [console_scripts]
    webirc-relay=txQWebIRCClient.examples.relay:run
    """

)