import hashlib
import json
import os
import re
import secrets
import string
import tempfile
from typing import Optional
from urllib.parse import urlparse

import boto3
import click
import magic
from botocore.exceptions import ClientError, NoCredentialsError
from bs4 import BeautifulSoup

s3_client = boto3.client("s3")


def tinyhost_main(
    html_files: list[str], bucket: Optional[str] = None, prefix: str = "", duration: int = 604800, reset: bool = False
):
    """
    Core logic that uploads an HTML file (or .ipynb) to S3 and returns signed URLs.
    This function does NOT rely on click, so you can call it from any Python code.

    :param html_files: List of html or ipynb file paths (or an HTTP link from tinyhost).
    :param bucket: S3 bucket name, if None we attempt to auto-detect/create one.
    :param prefix: S3 bucket prefix, defaults to "".
    :param duration: Expiration for the resulting link, default is 1 week (604800 seconds).
    :param reset: If True, resets the “datastore” portion inside <head>.
    :return: List of resulting signed URLs (one per file).
    """
    if isinstance(html_files, str):
        # If the caller passed a single string, coerce it into a list.
        html_files = [html_files]

    if not html_files:
        # Return empty right away
        return []

    # If bucket is None or empty, try to create/detect one
    if not bucket:
        bucket = run_new_bucket_flow()
        if not bucket:
            raise RuntimeError("Unable to automatically detect/create an S3 bucket, please specify one using --bucket")

    results = []

    for html_file in html_files:
        temp_file_name = None
        try:
            # If the user passed an existing tinyhost link, download it to a temporary file
            if re.match(r"^https?://", html_file, re.IGNORECASE):
                parsed = urlparse(html_file)
                domain_parts = parsed.netloc.split(".")
                # Basic attempt to parse bucket name from domain
                bucket_from_url = domain_parts[0]
                s3_key = parsed.path.lstrip("/")

                # We'll override the function's bucket with the one we just parsed
                bucket = bucket_from_url

                file_basename = os.path.splitext(os.path.basename(s3_key))[0].lower()
                # Strip out the final “-<12-char-hash>” if it exists
                file_basename = re.sub(r"(-[a-fA-F0-9]{12})?(\.\w+)?$", "", file_basename)
                file_extension = os.path.splitext(s3_key)[-1].lower()

                # Download the file from S3 to a local temp file
                with tempfile.NamedTemporaryFile("wb", suffix=file_extension, delete=False) as download_tmp:
                    s3_client.download_fileobj(bucket, s3_key, download_tmp)
                    downloaded_temp_file = download_tmp.name

                # Now we consider this downloaded file as our target
                html_file = downloaded_temp_file

            else:
                # Make sure local path exists
                if not os.path.exists(html_file):
                    raise FileNotFoundError(f"Path {html_file} does not exist")

                file_basename = os.path.splitext(os.path.basename(html_file))[0].lower()
                file_extension = os.path.splitext(html_file)[-1].lower()

            # Process HTML or ipynb
            if file_extension in [".htm", ".html"]:
                mime = magic.Magic(mime=True)
                content_type = mime.from_file(html_file)

                if content_type != "text/html":
                    raise ValueError("Your file was not detected as text/html.")

                # Insert or update the datastore script
                with open(html_file, "r") as f:
                    html_content = f.read()

                soup = BeautifulSoup(html_content, "html.parser")
                head_tag = soup.find("head")
                if not head_tag:
                    raise ValueError("Could not find <head> in your HTML. Please add one.")

                script_tags = head_tag.find_all("script")
                found_existing_template = False
                for script_tag in script_tags:
                    if script_tag.string and "BEGIN TINYHOST DATASTORE SECTION" in script_tag.string:
                        if reset:
                            datastore_id = generate_new_datastore()
                        else:
                            # Attempt to find existing datastoreId
                            datastore_re = re.search(r'const datastoreId = "(\w+)";', script_tag.string)
                            datastore_id = datastore_re[1] if datastore_re else generate_new_datastore()

                        get_url, post_dict = get_datastore_presigned_urls(bucket, prefix, datastore_id, duration)
                        script_tag.string = get_datastore_section(datastore_id, get_url, post_dict)
                        found_existing_template = True
                        break

                if not found_existing_template:
                    new_script = soup.new_tag("script")
                    datastore_id = generate_new_datastore()
                    get_url, post_dict = get_datastore_presigned_urls(bucket, prefix, datastore_id, duration)
                    new_script.string = get_datastore_section(datastore_id, get_url, post_dict)
                    head_tag.append(new_script)
                    head_tag.append(soup.new_string("\n"))

                html_content = str(soup)
                with open(html_file, "w") as f:
                    f.write(html_content)

            elif file_extension == ".ipynb":
                from nbconvert import HTMLExporter
                from nbformat import NO_CONVERT, read

                # Convert IPYNB to HTML
                with open(html_file, "r", encoding="utf-8") as f:
                    notebook_content = read(f, NO_CONVERT)

                html_exporter = HTMLExporter(template_name="classic")
                html_exporter.embed_images = True
                (body, resources) = html_exporter.from_notebook_node(notebook_content)

                # Write to a temp file
                with tempfile.NamedTemporaryFile("w", delete=False) as temp_file:
                    temp_file.write(body)
                    temp_file.flush()
                    temp_file_name = temp_file.name

                html_file = temp_file_name

            else:
                raise ValueError(
                    "You must use a .htm or .html extension for HTML pages, or .ipynb for Jupyter notebooks."
                )

            # Compute a short SHA1 hash for the final file
            sha1_hash = compute_sha1_hash(html_file)
            new_file_name = f"{file_basename}-{sha1_hash[:12]}{file_extension}"
            s3_key = f"{prefix}/{new_file_name}" if prefix else new_file_name

            # Upload to S3
            s3_client.upload_file(
                html_file,
                bucket,
                s3_key,
                ExtraArgs={"ContentType": "text/html", "CacheControl": "max-age=31536000, public"},
            )

            # Generate a signed URL
            signed_url = s3_client.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": s3_key}, ExpiresIn=duration
            )

            results.append(signed_url)

        except NoCredentialsError:
            raise RuntimeError("AWS credentials not found. Please configure them.")
        except Exception as exc:
            # Decide if you want to raise or just skip
            raise RuntimeError(f"Error while processing '{html_file}': {exc}") from exc
        finally:
            if temp_file_name:
                os.unlink(temp_file_name)

    return results


def generate_new_datastore():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))


def get_datastore_section(datastore_id, presigned_get_url, presigned_post_dict):
    # Path to your datastore_template.js, adjust as needed
    with open(os.path.join(os.path.dirname(__file__), "datastore_template.js"), "r") as f:
        template = f.read()

    # Simple string replacements
    template = template.replace("{{ datastore_id }}", datastore_id)
    template = template.replace("{{ presigned_get_url }}", presigned_get_url)
    template = template.replace("{{ presigned_post_dict }}", json.dumps(presigned_post_dict))

    # Optional indentation
    template = "\n" + template
    template = template.replace("\n", "\n    ").rstrip() + "\n"

    return template


def get_datastore_presigned_urls(bucket, prefix, datastore_id, duration):
    MAX_DATASTORE_SIZE = 2 * 1024 * 1024  # 2 MB
    object_key = f"{prefix}/{datastore_id}.json" if prefix else f"{datastore_id}.json"

    # Check if datastore object exists; if not, create it
    try:
        s3_client.head_object(Bucket=bucket, Key=object_key)
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            empty_json = json.dumps({})
            s3_client.put_object(Bucket=bucket, Key=object_key, Body=empty_json, ContentType="application/json")
        else:
            raise e

    get_url = s3_client.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": object_key}, ExpiresIn=duration
    )

    # POST is used for the writing side, because it's the only way to ensure a maximum length
    post_conditions = [
        ["content-length-range", 0, MAX_DATASTORE_SIZE],
    ]
    post_dict = s3_client.generate_presigned_post(
        Bucket=bucket, Key=object_key, Conditions=post_conditions, ExpiresIn=duration
    )
    return get_url, post_dict


def compute_sha1_hash(file_path):
    sha1 = hashlib.sha1()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            sha1.update(chunk)
    return sha1.hexdigest()


def run_new_bucket_flow():
    sts_client = boto3.client("sts")
    identity = sts_client.get_caller_identity()
    arn = identity["Arn"]
    username = arn.split("/")[-1]
    bucket = f"{username}-tinyhost"

    try:
        s3_client.head_bucket(Bucket=bucket)
        return bucket
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "404":
            s3_client.create_bucket(Bucket=bucket)
            return bucket
        else:
            raise RuntimeError(f"Error checking bucket existence: {e}")


@click.command()
@click.option("--bucket", help="S3 bucket on which to host your static site")
@click.option("--prefix", help="S3 bucket prefix to use", default="")
@click.option(
    "--reset",
    is_flag=True,
    show_default=True,
    default=False,
    help="Reset the data store back to an empty object",
)
@click.option(
    "--duration",
    default=604800,
    help="Length of time in seconds that the resulting link will work for. Default is 1 week.",
)
@click.argument("html_files", nargs=-1, type=str)
def tinyhost(html_files, bucket, prefix, duration, reset):
    """
    Hosts your html_files (or .ipynb's) on an S3 bucket, and gives back signed URLs.
    """

    if not html_files:
        # Equivalent of showing help
        click.echo(tinyhost.get_help(click.Context(tinyhost)))
        return

    try:
        urls = tinyhost_main(html_files=html_files, bucket=bucket, prefix=prefix, duration=duration, reset=reset)
        for url in urls:
            click.echo(f"\nAccess it at:\n{url}\n")
    except Exception as e:
        click.echo(str(e))


if __name__ == "__main__":
    tinyhost()
