from setuptools import setup

PLUGIN_NAME = "flyteinteractive"

microlib_name = f"flytekitplugins-{PLUGIN_NAME}"

plugin_requires = ["flytekit", "jupyter"]

__version__ = "0.0.0+develop"

setup(
    title="Flyte Interactive",
    title_expanded="Flytekit FlyteInteractive Plugin",
    name=microlib_name,
    version=__version__,
    author="flyteorg",
    author_email="admin@flyte.org",
    description="This package holds the flyteinteractive plugins for flytekit",
    namespace_packages=["flytekitplugins"],
    packages=[
        f"flytekitplugins.{PLUGIN_NAME}",
        f"flytekitplugins.{PLUGIN_NAME}.vscode_lib",
        f"flytekitplugins.{PLUGIN_NAME}.jupyter_lib",
    ],
    install_requires=plugin_requires,
    license="apache2",
    python_requires=">=3.9",
    classifiers=[
        "Intended Audience :: Science/Research",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Scientific/Engineering",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Software Development",
        "Topic :: Software Development :: Libraries",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    entry_points={"flytekit.plugins": [f"{PLUGIN_NAME}=flytekitplugins.{PLUGIN_NAME}"]},
)
