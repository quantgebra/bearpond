export interface Repository {
  name: string;
}

export interface FileMetadata {
  path: string;
  size: number;
  sha256: string;
}

export interface Manifest {
  seq: number;
  files: FileMetadata[];
}

export interface CommitRecord {
  commit_hash: string;
  seq: number;
  user: string | null;
  reason: string | null;
  committed_at: string;
  added: FileMetadata[];
  removed: FileMetadata[];
  updated: {
    path: string;
    old_size: number;
    old_sha256: string;
    new_size: number;
    new_sha256: string;
  }[];
}

export interface CommitHistoryPage {
  records: CommitRecord[];
}
