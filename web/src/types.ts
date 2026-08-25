export interface Repository {
  name: string;
}

export interface FileMetadata {
  path: string;
  sha256: string;
  size: number;
}

export interface UpdatedFileMetadata {
  path: string;
  old_sha256: string;
  old_size: number;
  new_sha256: string;
  new_size: number;
}

export interface Manifest {
  seq: number;
  created_at: string | null;
  files: FileMetadata[];
}

export interface CommitRecord {
  commit_hash: string;
  parent_commit_hash: string | null;
  txn_uuid: string;
  seq: number;
  committed_at: string;
  created_at: string | null;
  added: FileMetadata[];
  removed: FileMetadata[];
  updated: UpdatedFileMetadata[];
  user: string | null;
  reason: string | null;
}

export interface CommitHistoryPage {
  records: CommitRecord[];
  next_cursor: number | null;
}
