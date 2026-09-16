#[test]
fn first() {
    assert_eq!(candidate::api::add(1, 2), 3);
}

#[test]
fn next() {
    assert!(candidate::api::fresh());
}
