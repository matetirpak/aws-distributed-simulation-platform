//! Writes the replay first and the result last, each atomically: if `result.json` exists,
//! the output is complete. Zerobus and S3 come later, next to this.

use std::fs::{self, File};
use std::io::{self, BufReader, BufWriter, Write};
use std::path::Path;

use flate2::Compression;
use flate2::read::GzDecoder;
use flate2::write::GzEncoder;
use protocol::{MatchResult, Replay};

pub(crate) fn write(out_dir: &Path, replay: &Replay, result: &MatchResult) -> io::Result<()> {
    fs::create_dir_all(out_dir)?;

    let tmp = out_dir.join("replay.json.gz.tmp");
    let mut gz = GzEncoder::new(BufWriter::new(File::create(&tmp)?), Compression::default());
    serde_json::to_writer(&mut gz, replay)?;
    gz.finish()?.flush()?;
    fs::rename(&tmp, out_dir.join("replay.json.gz"))?;

    let tmp = out_dir.join("result.json.tmp");
    fs::write(&tmp, serde_json::to_vec_pretty(result)?)?;
    fs::rename(&tmp, out_dir.join("result.json"))
}

pub fn read_replay(path: &Path) -> io::Result<Replay> {
    let reader = GzDecoder::new(BufReader::new(File::open(path)?));
    Ok(serde_json::from_reader(reader)?)
}
