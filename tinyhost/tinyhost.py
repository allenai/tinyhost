import hashlib
import json
import os
import re
import secrets
import string
import tempfile

import boto3
import click
import magic
from botocore.exceptions import ClientError, NoCredentialsError
from bs4 import BeautifulSoup

# Create an S3 client using boto3
s3_client = boto3.client("s3")


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
    help="Length of time in seconds that this resulting link will work for. Default is 1 week. Max is also 1 week.",
)
@click.argument("html_files", nargs=-1, type=click.Path(exists=True))
def tinyhost(html_files: list[str], bucket: str, prefix: str, duration: int, reset: bool):
    """
    Hosts your html_files on an S3 bucket, and gives back signed URLs.

    Assumes that you have AWS credentials in your environment. Run `aws configure` if not.

    If you don't pass in an S3 bucket, the script will prompt you to create one, if it's possible.
    Otherwise, it will use the specified bucket
    """
    if not html_files:
        click.echo(tinyhost.get_help(click.Context(tinyhost)))
        return

    if not bucket:
        bucket = run_new_bucket_flow()

    if not bucket:
        raise click.ClickException(
            "Unable to automatically detect/create an S3 bucket, please specify one using --bucket"
        )

    # Loop over files
    for html_file in html_files:
        temp_file_name = None  # Initialize temp_file_name
        try:
            # Make sure that your file content is a text/html page to begin with
            file_basename = os.path.splitext(os.path.basename(html_file))[0].lower()
            file_extension = os.path.splitext(html_file)[-1].lower()

            if file_extension in [".htm", ".html"]:
                mime = magic.Magic(mime=True)
                content_type = mime.from_file(html_file)

                if content_type != "text/html":
                    raise click.ClickException("Your file was not detected as text/html.")

                with open(html_file, "r") as f:
                    html_content = f.read()

                soup = BeautifulSoup(html_content, "html.parser")

                head_tag = soup.find("head")

                # Write or update the datastore section
                if not head_tag:
                    raise click.ClickException("Could not find a <head> tag in your html, you'll need to add one")

                script_tags = head_tag.find_all("script")
                found_existing_template = False

                for script_tag in script_tags:
                    if script_tag.string and "BEGIN TINYHOST DATASTORE SECTION" in script_tag.string:
                        if reset:
                            datastore_id = generate_new_datastore()
                        else:
                            datastore_re = re.search(r'const datastoreId = "(\w+)";', script_tag.string)
                            datastore_id = datastore_re[1] if datastore_re else generate_new_datastore()

                        click.echo("Found existing datastore section, replacing...")

                        get_url, post_dict = get_datastore_presigned_urls(bucket, prefix, datastore_id, duration)
                        script_tag.string = get_datastore_section(datastore_id, get_url, post_dict)
                        found_existing_template = True
                        break

                if not found_existing_template:
                    click.echo("Need to write in new script template")
                    new_script = soup.new_tag("script")

                    datastore_id = generate_new_datastore()

                    get_url, post_dict = get_datastore_presigned_urls(bucket, prefix, datastore_id, duration)
                    new_script.string = get_datastore_section(datastore_id, get_url, post_dict)
                    head_tag.append(new_script)
                    head_tag.append(soup.new_string("\n"))

                html_content = str(soup)

                # Write the datastore back to the file, to help user debug and test if needed
                with open(html_file, "w") as f:
                    f.write(html_content)
            elif file_extension in [".ipynb"]:
                from nbconvert import HTMLExporter
                from nbformat import NO_CONVERT, read

                with open(html_file, "r", encoding="utf-8") as f:
                    notebook_content = read(f, NO_CONVERT)

                # Initialize the HTML exporter and specify the template
                html_exporter = HTMLExporter(template_name="classic")  # Specify template name here
                html_exporter.embed_images = True

                # Convert the notebook to HTML using the specified template
                (body, resources) = html_exporter.from_notebook_node(notebook_content)

                # Create a temporary file to hold the HTML content
                with tempfile.NamedTemporaryFile("w", delete=False) as temp_file:
                    temp_file.write(body)
                    temp_file.flush()
                    temp_file_name = temp_file.name

                html_file = temp_file_name
            else:
                raise click.ClickException(
                    "You must use a .htm or .html extension for html pages, or .ipynb for Jupyter notebooks"
                )

            sha1_hash = compute_sha1_hash(html_file)

            # Keep the original basename, so you can tell what to expect by looking at the URL
            new_file_name = f"{file_basename}-{sha1_hash[:12]}{file_extension}"

            s3_key = f"{prefix}/{new_file_name}" if prefix else new_file_name

            # Upload with a long cache, because our content is served via hashed link anyways
            # This helps when you have large single-page files, ex. with images embedded in them
            s3_client.upload_file(
                html_file,
                bucket,
                s3_key,
                ExtraArgs={"ContentType": "text/html", "CacheControl": "max-age=31536000, public"},
            )

            signed_url = s3_client.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": s3_key}, ExpiresIn=duration
            )

            if signed_url:
                click.echo(
                    f"Your file '{html_file}' has been uploaded successfully!\nAccess it via the following signed URL:\n\n{signed_url}\n"
                )
            else:
                click.echo(f"Failed to generate a signed URL for '{html_file}'.")

        except NoCredentialsError:
            click.echo("AWS credentials not found. Please configure them.")
        except Exception as e:
            click.echo(f"An error occurred while processing '{html_file}': {e}")
        finally:
            if temp_file_name:
                os.unlink(temp_file_name)


def generate_new_datastore() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for i in range(20))


def get_datastore_section(datastore_id: str, presigned_get_url: str, presigned_post_dict: dict[str, str]) -> str:
    with open(os.path.join(os.path.dirname(__file__), "datastore_template.js"), "r") as f:
        template = f.read()

    assert template.find('"{{ datastore_id }}"') != -1
    assert template.find('"{{ presigned_get_url }}"') != -1
    assert template.find("{{ presigned_post_dict }}") != -1

    template = template.replace("{{ datastore_id }}", datastore_id)
    template = template.replace("{{ presigned_get_url }}", presigned_get_url)
    template = template.replace("{{ presigned_post_dict }}", json.dumps(presigned_post_dict))

    # Make the format a little prettier
    template = "\n" + template
    template = template.replace("\n", "\n    ").rstrip() + "\n"

    return template


def get_datastore_presigned_urls(bucket: str, prefix: str, datastore_id: str, duration: int) -> tuple[str, dict]:
    MAX_DATASTORE_SIZE = 2 * 1024 * 1024  # 2 Megabytes

    # Only prepend `prefix/` if prefix is non-empty
    object_key = f"{prefix}/{datastore_id}.json" if prefix else f"{datastore_id}.json"

    # Check if object key exists, if not, make one, with the content {}
    # and the right ContentType
    try:
        s3_client.head_object(Bucket=bucket, Key=object_key)
        print(f"Object {object_key} exists.")
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            print(f"datastore {object_key} does not exist. Creating it.")
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


def compute_sha1_hash(file_path: str) -> str:
    sha1 = hashlib.sha1()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            sha1.update(chunk)

    return sha1.hexdigest()


def run_new_bucket_flow() -> str:
    sts_client = boto3.client("sts")
    identity = sts_client.get_caller_identity()
    arn = identity["Arn"]

    # Username is typically the last part of the ARN
    username = arn.split("/")[-1]
    bucket = f"{username}-tinyhost"

    # Search if bucket exists and is writable
    try:
        s3_client.head_bucket(Bucket=bucket)
        return bucket
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "404":
            click.echo(f"Bucket {bucket} does not exist, attempting to create")

            s3_client.create_bucket(Bucket=bucket)
            return bucket
        else:
            raise RuntimeError(f"Error checking bucket existence: {e}")


if __name__ == "__main__":
    tinyhost()
