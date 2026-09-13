//! Synthetic in-memory vector check. Never writes an unencrypted index sidecar.
fn main() {
    let mut index = turbovec::IdMapIndex::new(64, 4).expect("supported shape");
    let mut vectors = vec![0.0_f32; 3 * 64];
    vectors[0] = 1.0;
    vectors[64 + 1] = 1.0;
    vectors[128 + 2] = 1.0;
    index
        .add_with_ids(&vectors, &[10, 20, 30])
        .expect("valid vectors and stable IDs");
    let (scores, ids) = index.search(&vectors[..64], 1);
    assert_eq!(ids[0], 10);
    assert!(scores[0].is_finite());
    index.remove(10);
    let (_, ids) = index.search(&vectors[..64], 2);
    assert!(!ids.contains(&10));
    println!("PASS: 4-bit vector search and deletion; synthetic in-memory only");
}
