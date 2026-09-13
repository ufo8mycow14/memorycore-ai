fn main() {
    if std::env::var_os("CARGO_FEATURE_SQLCIPHER").is_some()
        && std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc")
    {
        // Vendored OpenSSL does not ship its optional compiler PDB. Missing
        // vendor debug symbols do not affect the statically linked release.
        println!("cargo:rustc-link-arg=/IGNORE:4099");
    }
}
