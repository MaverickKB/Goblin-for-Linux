import Foundation
import Security
import Darwin

struct CredentialRequest: Decodable {
    let operation: String
    let service: String
    let account: String
    let value: String?
}

struct CredentialFailure: Error {
    let message: String
    let authorizationRequired: Bool
}

func requireSuccess(_ status: OSStatus, operation: String) throws {
    if status == errSecSuccess { return }
    let authorizationRequired = status == errSecInteractionNotAllowed || status == errSecAuthFailed || status == errSecUserCanceled
    let message = authorizationRequired
        ? "Keychain access requires your authorization. Use Allow Keychain access in Connections."
        : "Keychain \(operation) failed (\(status))."
    throw CredentialFailure(message: message, authorizationRequired: authorizationRequired)
}

let appExecutableSuffix = "/Contents/MacOS/Goblin"
let engineExecutableSuffix = "/Contents/Resources/Runtime/Python/bin/python3"
let parentPathBufferSize = 4096


func parentExecutablePath() -> String {
    let parent = getppid()
    var buffer = [UInt8](repeating: 0, count: parentPathBufferSize)
    let length = buffer.withUnsafeMutableBytes { raw -> Int32 in
        return proc_pidpath(parent, raw.baseAddress, UInt32(raw.count))
    }
    if length <= 0 {
        return ""
    }
    let bytes = buffer.prefix(Int(length))
    return String(decoding: bytes, as: UTF8.self)
}


func requireAppParent() throws {
    let path = parentExecutablePath()
    let fromApp = path.hasSuffix(appExecutableSuffix)
    let fromEngine = path.hasSuffix(engineExecutableSuffix)
    if fromApp || fromEngine {
        return
    }
    throw CredentialFailure(message: "Credential request is outside the application.",
                            authorizationRequired: false)
}


func perform(_ request: CredentialRequest) throws -> [String: String] {
    try requireAppParent()
    guard let resources = Bundle.main.resourceURL else {
        throw CredentialFailure(message: "Credential service metadata is missing.", authorizationRequired: false)
    }
    let data = try Data(contentsOf: resources.appendingPathComponent("keychain-services.json"))
    let services = try JSONDecoder().decode([String: String].self, from: data)
    guard services.values.contains(request.service), !request.account.isEmpty else {
        throw CredentialFailure(message: "Credential item is outside this application.", authorizationRequired: false)
    }
    let interactive = request.operation == "authorize"
    try requireSuccess(SecKeychainSetUserInteractionAllowed(interactive), operation: "interaction policy")
    var query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: request.service,
        kSecAttrAccount as String: request.account,
        kSecUseAuthenticationUI as String: interactive ? kSecUseAuthenticationUIAllow : kSecUseAuthenticationUIFail
    ]
    switch request.operation {
    case "get", "authorize":
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return ["state": "missing"] }
        try requireSuccess(status, operation: "read")
        if interactive { return ["state": "authorized"] }
        guard let data = result as? Data else {
            throw CredentialFailure(message: "Credential data is unavailable.", authorizationRequired: false)
        }
        return ["state": "available", "value": String(decoding: data, as: UTF8.self)]
    case "exists":
        let status = SecItemCopyMatching(query as CFDictionary, nil)
        if status == errSecItemNotFound { return ["state": "missing"] }
        try requireSuccess(status, operation: "status")
        return ["state": "found"]
    case "put":
        guard let value = request.value, !value.isEmpty else {
            throw CredentialFailure(message: "Credential value is missing.", authorizationRequired: false)
        }
        let bytes = Data(value.utf8)
        let status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: bytes] as CFDictionary)
        if status == errSecItemNotFound {
            var item = query
            item.removeValue(forKey: kSecUseAuthenticationUI as String)
            item[kSecValueData as String] = bytes
            try requireSuccess(SecItemAdd(item as CFDictionary, nil), operation: "save")
        } else {
            try requireSuccess(status, operation: "update")
        }
        var verification = query
        verification[kSecReturnData as String] = true
        var result: CFTypeRef?
        try requireSuccess(SecItemCopyMatching(verification as CFDictionary, &result), operation: "verification")
        guard let stored = result as? Data, stored == bytes else {
            throw CredentialFailure(message: "Credential verification failed.", authorizationRequired: false)
        }
        return ["state": "saved"]
    case "delete":
        let status = SecItemDelete(query as CFDictionary)
        if status != errSecItemNotFound { try requireSuccess(status, operation: "delete") }
        return ["state": "deleted"]
    default:
        throw CredentialFailure(message: "Unknown credential operation.", authorizationRequired: false)
    }
}

var response: [String: String]
do {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard input.count <= 65_536 else {
        throw CredentialFailure(message: "Credential request is too large.", authorizationRequired: false)
    }
    let request = try JSONDecoder().decode(CredentialRequest.self, from: input)
    response = try perform(request)
} catch let error as CredentialFailure {
    response = ["state": "error", "message": error.message,
                "code": error.authorizationRequired ? "authorization_required" : "credential_error"]
} catch {
    response = ["state": "error", "message": "The credential request could not be completed.", "code": "credential_error"]
}
if let data = try? JSONEncoder().encode(response) {
    FileHandle.standardOutput.write(data)
}
