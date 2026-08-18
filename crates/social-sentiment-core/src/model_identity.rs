use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{BufReader, Read};
use std::path::Path;

/// Return the lowercase SHA-256 digest for a model artifact on disk.
pub fn sha256_file<P: AsRef<Path>>(path: P) -> anyhow::Result<String> {
    let file = File::open(path.as_ref())
        .map_err(|e| anyhow::anyhow!("Failed to open model artifact {:?}: {}", path.as_ref(), e))?;
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 1024 * 1024];

    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::NamedTempFile;

    #[test]
    fn test_sha256_file_matches_known_hash() {
        let mut tmp = NamedTempFile::new().unwrap();
        tmp.write_all(b"test model content").unwrap();
        tmp.flush().unwrap();

        let hash = sha256_file(tmp.path()).unwrap();
        assert_eq!(hash.len(), 64);
        assert_eq!(hash, "8cf3a78cc64a1d9952a895d574d82ce37ad3b4328893e97dff9611fe3e52706d");
    }
}
