Kotlin provides all their documentation in a Git repo. https://github.com/JetBrains/kotlin-web-site

It's in Markdown and XML format.

We need to use the ProcessDocs/ProcessKotlinDocs/InitWriterside/init_writerside.sh script before processing the docs. This changes two XML config files. And records the commit hash of the Kotlin site.
Run it through a tool called "Writerside" (also from JetBrains - https://www.jetbrains.com/writerside/). Converts into HTML.
Must be run from inside PyCharm with the Writerside plugin. Emites writerside_log.txt file with progress and any errors.
Produces two zip files - one for text and the other for images. Technically, the images zip needs to opened inside (nest) of the result of unziping the text zip.
Runs quickly, just a minute or two.
Errors in the output indicate broken links that would point to missing topics.


This folder contains the resulting output HTML.

See also https://appdevforall.atlassian.net/wiki/spaces/SD/pages/253231117/ADFA+Offline+Documentation+Tools+documentation
