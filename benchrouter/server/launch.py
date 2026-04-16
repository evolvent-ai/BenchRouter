import uvicorn
import argparse
import os


def main():
    parser = argparse.ArgumentParser(description="Launch BenchRouter Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=9000, help="Port to bind")
    parser.add_argument(
        "--data-dir",
        default="/data/benchrouter",
        help="Directory for storing benchmarks, environments, results",
    )
    args = parser.parse_args()

    os.environ["BENCHROUTER_DATA_DIR"] = args.data_dir

    print(f"Starting BenchRouter Server on {args.host}:{args.port}")
    print(f"Data directory: {args.data_dir}")
    uvicorn.run(
        "benchrouter.server.api:app", host=args.host, port=args.port, reload=False
    )


if __name__ == "__main__":
    main()
