# tinyhost

Have you ever wanted to host a webpage? Me too!

Except no one wants to host their own server these days. Too much trouble keeping that thing secure, updated, properly firewalled. And to be honest, it's overkill for hosting just a tiny simple page.

You could just upload a static page to S3, but for a backend you'd need some serverless thing like AWS Lambda functions. But even those would need some sort of way to store your data...

Introducing, serverless-less web hosting with tinyhost!

Just type:

```
pip install tinyhost

tinyhost mystaticpage.html
```

Then share the link with your trusted friends and coworkers!

tinyhost will put your static page on S3, and configure it with a trusted backend... also hosted on S3! It's literally S3 all the way down.