(function_item
  name: (identifier) @name
  parameters: (parameters) @params
  return_type: (type_specifier)? @return_type) @function

(struct_item
  name: (type_identifier) @name) @class

(enum_item
  name: (type_identifier) @name) @class

(trait_item
  name: (type_identifier) @name) @class

(impl_item
  trait: (type_identifier)? @trait
  type: (type_identifier) @name) @class

(mod_item
  name: (identifier) @name) @module

(let_declaration
  pattern: (identifier) @name) @variable
