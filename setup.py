from setuptools import setup, find_packages

setup(
    name="membase",
    version="0.1.9",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    package_data={
        "membase": ["chain/solc/**/*"],
    },

    description="python sdk for membase operation: memory, knowledge, chain, auth etc.",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="",
    author_email="",
    python_requires=">=3.10",
    install_requires=[],
) 