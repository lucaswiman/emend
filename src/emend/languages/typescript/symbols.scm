(function_declaration
  name: (identifier) @name
  parameters: (formal_parameters) @params
  return_type: (type_annotation)? @return_type) @function

(class_declaration
  name: (identifier) @name) @class

(method_definition
  name: (property_identifier) @name
  parameters: (formal_parameters) @params
  return_type: (type_annotation)? @return_type) @method

(lexical_declaration
  (variable_declarator
    name: (identifier) @name)) @variable

(variable_declaration
  (variable_declarator
    name: (identifier) @name)) @variable
