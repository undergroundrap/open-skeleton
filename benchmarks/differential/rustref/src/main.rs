//! Reference reader: parses one Rust file with `syn` and prints what it declares.
//!
//! This exists only to disagree with the lexical analyzer. Anything it reports
//! that the analyzer misses is a defect in the analyzer; anything the analyzer
//! reports that this does not is a fabrication.

use quote::ToTokens;
use syn::visit::{self, Visit};
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

fn walk(
    items: &[Item],
    names: &mut Vec<String>,
    decls: &mut Vec<String>,
    impls: &mut Vec<(String, String)>,
    consts: &mut Vec<(String, String)>,
) {
    for item in items {
        match item {
            Item::Fn(node) => {
                names.push(node.sig.ident.to_string());
                decls.push(node.sig.ident.to_string());
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
                walk(&nested, names, decls, impls, consts);
            }
            Item::Struct(node) => {
                names.push(node.ident.to_string());
                decls.push(node.ident.to_string());
            }
            Item::Enum(node) => {
                names.push(node.ident.to_string());
                decls.push(node.ident.to_string());
            }
            Item::Trait(node) => {
                names.push(node.ident.to_string());
                decls.push(node.ident.to_string());
                // A trait's method signatures are declarations too. Walking
                // only impl bodies reported them as absent, which made the
                // lexical reader look like it was inventing a dozen names per
                // trait definition.
                for sub in &node.items {
                    if let syn::TraitItem::Fn(method) = sub {
                        names.push(method.sig.ident.to_string());
                    }
                }
            }
            Item::Union(node) => {
                names.push(node.ident.to_string());
                decls.push(node.ident.to_string());
            }
            Item::Mod(node) => {
                if let Some((_, inner)) = &node.content {
                    walk(inner, names, decls, impls, consts);
                }
            }
            Item::Const(node) => {
                consts.push((node.ident.to_string(), type_name(&node.ty)));
            }
            Item::Static(node) => {
                consts.push((node.ident.to_string(), type_name(&node.ty)));
            }
            Item::Impl(node) => {
                let owner = type_name(&node.self_ty);
                if let Some((_, path, _)) = &node.trait_ {
                    if let Some(segment) = path.segments.last() {
                        impls.push((owner.clone(), segment.ident.to_string()));
                    }
                }
                for sub in &node.items {
                    if let syn::ImplItem::Const(item) = sub {
                        // An associated constant is still a constant. Walking
                        // only module scope reported real ones as absent.
                        consts.push((item.ident.to_string(), type_name(&item.ty)));
                    }
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
                        walk(&nested, names, decls, impls, consts);
                    }
                }
            }
            _ => {}
        }
    }
}

/// Collects the names invoked as calls, which is what the lexical reader
/// claims to find. A method call contributes its method name and a path call
/// its final segment, matching how a name-only reader can see them.
#[derive(Default)]
struct Calls {
    names: Vec<String>,
    consts: Vec<(String, String)>,
}

impl<'ast> Visit<'ast> for Calls {
    fn visit_expr_call(&mut self, node: &'ast syn::ExprCall) {
        if let syn::Expr::Path(path) = &*node.func {
            if let Some(segment) = path.path.segments.last() {
                self.names.push(segment.ident.to_string());
            }
        }
        visit::visit_expr_call(self, node);
    }

    fn visit_expr_method_call(&mut self, node: &'ast syn::ExprMethodCall) {
        self.names.push(node.method.to_string());
        visit::visit_expr_method_call(self, node);
    }

    fn visit_item_const(&mut self, node: &'ast syn::ItemConst) {
        // A constant can be declared inside a closure or an `if` block, which
        // no walk over top-level statements reaches. The visitor sees them all.
        self.consts.push((node.ident.to_string(), type_name(&node.ty)));
        visit::visit_item_const(self, node);
    }

    fn visit_item_static(&mut self, node: &'ast syn::ItemStatic) {
        self.consts.push((node.ident.to_string(), type_name(&node.ty)));
        visit::visit_item_static(self, node);
    }

    fn visit_impl_item_const(&mut self, node: &'ast syn::ImplItemConst) {
        self.consts.push((node.ident.to_string(), type_name(&node.ty)));
        visit::visit_impl_item_const(self, node);
    }

    fn visit_item_macro(&mut self, _node: &'ast syn::ItemMacro) {
        // A macro body is a template. The lexical reader skips it and so does
        // this, or every disagreement would be about code nobody wrote.
    }

    fn visit_macro(&mut self, _node: &'ast syn::Macro) {}
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
    let mut calls = Calls::default();
    calls.visit_file(&parsed);
    let mut names = Vec::new();
    let mut decls = Vec::new();
    let mut impls = Vec::new();
    let mut consts = Vec::new();
    walk(&parsed.items, &mut names, &mut decls, &mut impls, &mut consts);
    let pairs: Vec<_> = impls
        .into_iter()
        .map(|(owner, trait_name)| json!({"owner": owner, "trait": trait_name}))
        .collect();
    println!("{}", json!({"parsed": true, "names": names, "decls": decls, "impls": pairs, "calls": calls.names,
        "consts": calls.consts.iter().map(|(n, t)| json!({"name": n, "type": t})).collect::<Vec<_>>()}));
}
