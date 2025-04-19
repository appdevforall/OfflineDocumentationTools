# The root of documentation in the Kotlin website repo is "docs"
DOC_DIRNAME="docs"

if [ "$#" -ne 2 ]; then
  echo "Usage: ./init_writerside.sh /path/to/kotlin-site /path/to/out/dir"
  exit
fi

site_path=$1
out_dir=$2

if [ ! -d $2 ]; then
  mkdir -p $out_dir
fi

# Copy files over
cp -r ${site_path}/${DOC_DIRNAME} $out_dir

# Copy in updated build profiles for offline distribution
script_dir=$(dirname $(realpath -s $0))
cp ${script_dir}/build-script.xml ${out_dir}/${DOC_DIRNAME}/cfg/build-script.xml
cp ${script_dir}/buildprofiles.xml ${out_dir}/${DOC_DIRNAME}/cfg/buildprofiles.xml

# Get commit hash
cd $site_path
echo $(git rev-parse HEAD) >> "${out_dir}/docs_version.txt"