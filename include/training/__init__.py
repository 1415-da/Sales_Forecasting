"""
Training-data contract shared by the API and the training DAG.

One implementation of "what a training dataset is", imported by both sides, so
the upload the API accepted is exactly the dataset the DAG trains on: the same
schema, the same validation, the same content hash.
"""
