//! Reference reader: parses one Rust file with `syn` and prints what it declares.
//!
//! This exists only to disagree with the lexical analyzer. Anything it reports
//! that the analyzer misses is a defect in the analyzer; anything the analyzer
//! reports that this does not is a fabrication.

use quote::ToTokens;
use serde_json::json;
use std::{env, fs};
use syn::{Item, Type};

fn type_name(ty: &Type) -> String {
    match ty {
        Type::Path(path) => path
            .path
            .segments
            .last()
            .map(|s| s.ident.to_string())
            .unwrap_or_default(),
        Type::Reference(inner) => type_name(&inner.elem),
        other => other.to_token_stream().to_string().replace(' ', ""),
    }
}

fn walk(items: &[Item], names: &mut Vec<String>, impls: &mut Vec<(String, String)>) {
    for item in items {
        match item {
            Item::Fn(node) => {
                names.push(node.sig.ident.to_string());
                // Rust allows items inside a function body, and serde declares
                // visitor structs and their impls there. Walking only modules
                // reported those implementations as absent, which would have
                // made the lexical analyzer look like it was inventing them.
                let nested: Vec<Item> = node
                    .block
                    .stmts
                    .iter()
                    .filter_map(|stmt| match stmt {
                        syn::Stmt::Item(item) => Some(item.clone()),
                        _ => None,
                    })
                    .collect();
                walk(&nested, names, impls);
            }
            Item::Struct(node) => names.push(node.ident.to_string()),
            Item::Enum(node) => names.push(node.ident.to_string()),
            Item::Trait(node) => names.push(node.ident.to_string()),
            Item::Union(node) => names.push(node.ident.to_string()),
            Item::Mod(node) => {
                if let Some((_, inner)) = &node.content {
                    walk(inner, names, impls);
                }
            }
            Item::Impl(node) => {
                let owner = type_name(&node.self_ty);
                if let Some((_, path, _)) = &node.trait_ {
                    if let Some(segment) = path.segments.last() {
                        impls.push((owner.clone(), segment.ident.to_string()));
                    }
                }
                for sub in &node.items {
                    if let syn::ImplItem::Fn(method) = sub {
                        names.push(method.sig.ident.to_string());
                        let nested: Vec<Item> = method
                            .block
                            .stmts
                            .iter()
                            .filter_map(|stmt| match stmt {
                                syn::Stmt::Item(item) => Some(item.clone()),
                                _ => None,
                            })
                            .collect();
                        walk(&nested, names, impls);
                    }
                }
            }
            _ => {}
        }
    }
}

fn main() {
    let path = env::args().nth(1).expect("usage: rustref <file.rs>");
    let source = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(_) => {
            println!("{}", json!({"parsed": false}));
            return;
        }
    };
    let parsed = match syn::parse_file(&source) {
        Ok(file) => file,
        Err(_) => {
            println!("{}", json!({"parsed": false}));
            return;
        }
    };
    let mut names = Vec::new();
    let mut impls = Vec::new();
    walk(&parsed.items, &mut names, &mut impls);
    let pairs: Vec<_> = impls
        .into_iter()
        .map(|(owner, trait_name)| json!({"owner": owner, "trait": trait_name}))
        .collect();
    println!("{}", json!({"parsed": true, "names": names, "impls": pairs}));
}
