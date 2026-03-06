(function_definition
  name: (identifier) @name
  parameters: (parameters) @params
  return_type: (type)? @return_type) @function

(class_definition
  name: (identifier) @name) @class

(decorated_definition
  definition: [
    (function_definition
      name: (identifier) @name
      parameters: (parameters) @params
      return_type: (type)? @return_type)
    (class_definition
      name: (identifier) @name)
  ]) @decorated

(assignment
  left: (identifier) @name) @variable
