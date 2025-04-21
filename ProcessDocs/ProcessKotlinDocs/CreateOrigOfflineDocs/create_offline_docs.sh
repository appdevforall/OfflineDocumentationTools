if [ "$#" -ne 2 ]; then
  echo "Usage: ./create_offline_docs.sh /path/to/WritersideOutput /path/to/out/dir\nConvert Writerside output to offlin webhelp/"
  exit
fi

iw_out=${1}
out_dir=${2}

if [ ! -d out_dir ]; then
  mkdir -p $out_dir
fi

# Copy over Writerside output
cp ${iw_out}/docs_version.txt ${out_dir}
cp ${iw_out}/webHelpImages.zip ${out_dir}
cp ${iw_out}/webHelpKR2.zip ${out_dir}

# Unzip Writerside output into correct folders for webhelp
cd ${out_dir}
mkdir html
mv webHelpKR2.zip html
mv webHelpImages.zip html
cd html
unzip webHelpKR2.zip
rm webHelpKR2.zip
mkdir images
mv webHelpImages.zip images
cd images
unzip webHelpImages.zip
rm webHelpImages.zip