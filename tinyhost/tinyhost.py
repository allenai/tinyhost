import hashlib
import os
import boto3
import click
import tempfile
import magic

from bs4 import BeautifulSoup

from botocore.exceptions import NoCredentialsError

# Create an S3 client using boto3
s3_client = boto3.client('s3')

@click.command()
@click.option("--bucket", help="S3 bucket on which to host your static site", required=True)
@click.option("--prefix", help="S3 bucket prefix to use", default="")
@click.option("--duration", default=604800, help="Length of time in seconds that this resulting link will work for. Default is 1 week. Max is also 1 week.")
@click.argument("html_file", type=click.Path(exists=True))
def tinyhost(html_file: str, bucket: str, prefix: str, duration: int):
    """
    Hosts your html_file on an S3 bucket, and gives back a signed URL.
    """
    try:
        # Make sure that your file content is a text/html page to begin with
        file_extension = os.path.splitext(html_file)[-1]

        if file_extension.lower() not in [".htm", ".html"]:
            raise click.ClickException("You must use a .htm or .html extension")

        mime = magic.Magic(mime=True)
        content_type = mime.from_file(html_file)

        if content_type != "text/html":
            raise click.ClickException("Your file was not detected as text/html.")
        
        with open(html_file, "r") as f:
            html_content = f.read()
        
        soup = BeautifulSoup(html_content, "html.parser")

        new_script = soup.new_tag("script")
        new_script.string = 'console.log("Hi");'
        body_tag = soup.find("body")

        # Write or update the datastore section
        if not body_tag:
            raise click.ClickException("Could not find a <body> tag in your html.")
        
        script_tags = body_tag.find_all("script")
        found_existing_template = False

        for script_tag in script_tags:
            if script_tag.string and "BEGIN TINYHOST DATASTORE SECTION" in script_tag.string:
                script_tag.string = get_datastore_section("abcd", "", "")
                found_existing_template = True
                break

        if not found_existing_template:
            new_script = soup.new_tag("script")
            new_script.string = get_datastore_section("abc", "", "")
            body_tag.append(new_script)

        html_content = str(soup)

        # Write the data back to the file
        with open(html_file, "w") as f:
            f.write(html_content)

        sha1_hash = compute_sha1_hash(html_file)
        
        new_file_name = f"{sha1_hash}{file_extension}"

        s3_key = f"{prefix}/{new_file_name}" if prefix else new_file_name
        upload_file_to_s3(html_file, bucket, s3_key)

        signed_url = create_presigned_url(bucket, s3_key, expiration=duration)

        if signed_url:
            click.echo(f"Your file has been uploaded successfully!\nAccess it via the following signed URL:\n\n{signed_url}")
        else:
            click.echo("Failed to generate a signed URL.")

    except NoCredentialsError:
        click.echo("AWS credentials not found. Please configure them.")


def get_datastore_section(datastore_id: str, presigned_get_url: str, presigned_put_url: str) -> str:
    with open(os.path.join(os.path.dirname(__file__), "datastore_template.html"), "r") as f:
        template = f.read()

    print(template)

    assert template.find("\"{{ datastore_id }}\"") != -1
    assert template.find("\"{{ presigned_get_url }}\"") != -1
    assert template.find("\"{{ presigned_put_url }}\"") != -1

    template = template.replace("{{ datastore_id }}", datastore_id)
    template = template.replace("{{ presigned_get_url }}", presigned_get_url)
    template = template.replace("{{ presigned_put_url }}", presigned_put_url)

    return template

def compute_sha1_hash(file_path: str) -> str:
    sha1 = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            sha1.update(chunk)
    return sha1.hexdigest()

def upload_file_to_s3(file_path: str, bucket: str, s3_key: str):
    try:
        s3_client.upload_file(file_path, bucket, s3_key,
                               ExtraArgs={'ContentType': 'text/html'})
        click.echo(f"File uploaded successfully to S3: {s3_key}")
    except Exception as e:
        raise RuntimeError(f"Failed to upload file to S3: {e}")

def create_presigned_url(bucket: str, object_key: str, expiration: int = 3600) -> str:
    try:
        response = s3_client.generate_presigned_url('get_object',
                                                    Params={'Bucket': bucket, 'Key': object_key},
                                                    ExpiresIn=expiration)
        return response
    except Exception as e:
        raise RuntimeError(f"Failed to create presigned URL: {e}")

if __name__ == "__main__":
    tinyhost()
