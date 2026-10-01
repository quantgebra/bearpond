# bearpond-protocol

The shared wire-format types for [bearpond](https://github.com/quantgebra/bearpond), a small transactional data lake.

This package holds only the pydantic models that the bearpond server and client exchange over HTTP, such as declared files, transaction manifests and commit records. It has no dependency on either side and contains no behavior beyond validation, so the client and server stay independently deployable while agreeing exactly on the format.

You normally don't install it directly: [`bearpond`](https://github.com/quantgebra/bearpond/tree/main/packages/client) (the client) and [`bearpond-server`](https://github.com/quantgebra/bearpond/tree/main/packages/server) both depend on it.

Requires Python 3.11+.

```bash
pip install bearpond-protocol
```

## License

Apache License 2.0.
