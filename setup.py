from setuptools import setup


setup(
    name="podcast-transcript-workflow",
    version="0.1.0",
    description=(
        "Local-first podcast subscription, transcription, readable transcript, "
        "and optional Feishu/Lark publishing workflow."
    ),
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    python_requires=">=3.9",
    packages=["podcast_tracker"],
    entry_points={
        "console_scripts": [
            "podcast-tracker=podcast_tracker.cli:main",
        ],
    },
)
