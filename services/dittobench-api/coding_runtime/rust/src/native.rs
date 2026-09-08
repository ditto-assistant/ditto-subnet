//! Sealed conversions used only by the generated, confined candidate bridge.
use crate::value::{Data, Integer, Type, Value};

mod sealed {
    pub trait Sealed {}
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ConversionError;
type Result<T> = std::result::Result<T, ConversionError>;

pub trait NativeValue: sealed::Sealed {
    fn kind() -> Type;
    fn to_value(&self) -> Result<Value>;
}
pub trait NativeDecode: NativeValue + Sized {
    fn from_value(value: &Value) -> Result<Self>;
}
/// Borrowed text points only into this request's owned data in the confined
/// bridge process. The Rust lifetime prevents use after request disposal.
/// ```compile_fail
/// use coding_rust_suite::{native::BorrowDecode,value::Value};
/// fn escape(value: Value) -> &'static str {
///     <&str as BorrowDecode>::from_borrowed(&value).unwrap()
/// }
/// ```
pub trait BorrowDecode<'value>: NativeValue + Sized {
    fn from_borrowed(value: &'value Value) -> Result<Self>;
}
/// Fixed per-process budget for static text required by approved input ABIs.
/// Allocations live only in the confined candidate process, reclaimed on reap.
pub struct StaticTextBudget {
    bytes: usize,
    strings: usize,
}
impl Default for StaticTextBudget {
    fn default() -> Self {
        Self {
            bytes: 4 << 20,
            strings: 262_144,
        }
    }
}
impl StaticTextBudget {
    fn copy(&mut self, text: &str) -> Result<&'static str> {
        if text.len() > self.bytes || self.strings == 0 {
            return Err(ConversionError);
        }
        self.bytes -= text.len();
        self.strings -= 1;
        Ok(Box::leak(text.to_owned().into_boxed_str()))
    }
}
pub trait StaticTextDecode: NativeValue + Sized {
    fn from_static_text(value: &Value, budget: &mut StaticTextBudget) -> Result<Self>;
}
fn make(kind: Type, data: Data) -> Result<Value> {
    Value::new(kind, data).map_err(|_| ConversionError)
}
fn typed<T: NativeValue + ?Sized>(value: &Value) -> Result<&Data> {
    if value.kind() != &T::kind() {
        return Err(ConversionError);
    }
    Ok(value.data())
}
fn collect<'a, T: NativeValue + 'a>(values: impl Iterator<Item = &'a T>) -> Result<Vec<Value>> {
    let (mut nodes, mut bytes) = (1usize, 16usize);
    let mut output = Vec::new();
    for value in values {
        let value = value.to_value()?;
        let (n, b) = value.cost();
        nodes += n;
        bytes += b;
        if nodes > 4096 || bytes > 65536 {
            return Err(ConversionError);
        }
        output.push(value);
    }
    Ok(output)
}

macro_rules! scalar {
    ($ty:ty, $kind:expr, $data:ident) => {
        impl sealed::Sealed for $ty {}
        impl NativeValue for $ty {
            fn kind() -> Type {
                $kind
            }
            fn to_value(&self) -> Result<Value> {
                make(Self::kind(), Data::$data(*self))
            }
        }
        impl NativeDecode for $ty {
            fn from_value(value: &Value) -> Result<Self> {
                if let Data::$data(value) = typed::<Self>(value)? {
                    Ok(*value)
                } else {
                    Err(ConversionError)
                }
            }
        }
        impl<'value> BorrowDecode<'value> for $ty {
            fn from_borrowed(value: &'value Value) -> Result<Self> {
                Self::from_value(value)
            }
        }
        impl StaticTextDecode for $ty {
            fn from_static_text(value: &Value, _budget: &mut StaticTextBudget) -> Result<Self> {
                Self::from_value(value)
            }
        }
    };
}
scalar!(bool, Type::Bool, Bool);
scalar!(char, Type::Char, Char);
macro_rules! integer {
    ($ty:ty, $kind:ident) => {
        impl sealed::Sealed for $ty {}
        impl NativeValue for $ty {
            fn kind() -> Type {
                Type::Int(Integer::$kind)
            }
            fn to_value(&self) -> Result<Value> {
                make(Self::kind(), Data::Int(*self as i128))
            }
        }
        impl NativeDecode for $ty {
            fn from_value(value: &Value) -> Result<Self> {
                if let Data::Int(value) = typed::<Self>(value)? {
                    Self::try_from(*value).map_err(|_| ConversionError)
                } else {
                    Err(ConversionError)
                }
            }
        }
        impl<'value> BorrowDecode<'value> for $ty {
            fn from_borrowed(value: &'value Value) -> Result<Self> {
                Self::from_value(value)
            }
        }
        impl StaticTextDecode for $ty {
            fn from_static_text(value: &Value, _budget: &mut StaticTextBudget) -> Result<Self> {
                Self::from_value(value)
            }
        }
    };
}
integer!(u8, U8);
integer!(u16, U16);
integer!(u32, U32);
integer!(u64, U64);
integer!(usize, Usize);
integer!(i8, I8);
integer!(i16, I16);
integer!(i32, I32);
integer!(i64, I64);
integer!(isize, Isize);

impl sealed::Sealed for str {}
impl NativeValue for str {
    fn kind() -> Type {
        Type::Text
    }
    fn to_value(&self) -> Result<Value> {
        if self.len() > 65520 {
            return Err(ConversionError);
        }
        make(Self::kind(), Data::Text(self.to_owned()))
    }
}
impl sealed::Sealed for String {}
impl NativeValue for String {
    fn kind() -> Type {
        Type::Text
    }
    fn to_value(&self) -> Result<Value> {
        self.as_str().to_value()
    }
}
impl NativeDecode for String {
    fn from_value(value: &Value) -> Result<Self> {
        if let Data::Text(value) = typed::<Self>(value)? {
            Ok(value.clone())
        } else {
            Err(ConversionError)
        }
    }
}
impl<'value> BorrowDecode<'value> for String {
    fn from_borrowed(value: &'value Value) -> Result<Self> {
        Self::from_value(value)
    }
}
impl StaticTextDecode for String {
    fn from_static_text(value: &Value, _budget: &mut StaticTextBudget) -> Result<Self> {
        Self::from_value(value)
    }
}
impl StaticTextDecode for &'static str {
    fn from_static_text(value: &Value, budget: &mut StaticTextBudget) -> Result<Self> {
        budget.copy(<&str as BorrowDecode>::from_borrowed(value)?)
    }
}
impl<'value> BorrowDecode<'value> for &'value str {
    fn from_borrowed(value: &'value Value) -> Result<Self> {
        let Data::Ref(inner) = typed::<Self>(value)? else {
            return Err(ConversionError);
        };
        let Data::Text(text) = typed::<str>(inner)? else {
            return Err(ConversionError);
        };
        Ok(text.as_str())
    }
}
impl<T: NativeValue + ?Sized> sealed::Sealed for &T {}
impl<T: NativeValue + ?Sized> NativeValue for &T {
    fn kind() -> Type {
        Type::Ref(Box::new(T::kind()))
    }
    fn to_value(&self) -> Result<Value> {
        make(Self::kind(), Data::Ref(Box::new(T::to_value(*self)?)))
    }
}
impl<T: NativeValue> sealed::Sealed for [T] {}
impl<T: NativeValue> NativeValue for [T] {
    fn kind() -> Type {
        Type::Slice(Box::new(T::kind()))
    }
    fn to_value(&self) -> Result<Value> {
        if self.len() > 4095 {
            return Err(ConversionError);
        }
        make(Self::kind(), Data::Sequence(collect(self.iter())?))
    }
}
impl<T: NativeValue> sealed::Sealed for Vec<T> {}
impl<T: NativeValue> NativeValue for Vec<T> {
    fn kind() -> Type {
        Type::Vec(Box::new(T::kind()))
    }
    fn to_value(&self) -> Result<Value> {
        if self.len() > 4095 {
            return Err(ConversionError);
        }
        make(Self::kind(), Data::Sequence(collect(self.iter())?))
    }
}
impl<T: NativeDecode> NativeDecode for Vec<T> {
    fn from_value(value: &Value) -> Result<Self> {
        if let Data::Sequence(values) = typed::<Self>(value)? {
            values.iter().map(T::from_value).collect()
        } else {
            Err(ConversionError)
        }
    }
}
impl<'value, T: BorrowDecode<'value>> BorrowDecode<'value> for Vec<T> {
    fn from_borrowed(value: &'value Value) -> Result<Self> {
        if let Data::Sequence(values) = typed::<Self>(value)? {
            values.iter().map(T::from_borrowed).collect()
        } else {
            Err(ConversionError)
        }
    }
}
impl<T: StaticTextDecode> StaticTextDecode for Vec<T> {
    fn from_static_text(value: &Value, budget: &mut StaticTextBudget) -> Result<Self> {
        if let Data::Sequence(values) = typed::<Self>(value)? {
            values
                .iter()
                .map(|v| T::from_static_text(v, budget))
                .collect()
        } else {
            Err(ConversionError)
        }
    }
}
impl<T: NativeValue, const N: usize> sealed::Sealed for [T; N] {}
impl<T: NativeValue, const N: usize> NativeValue for [T; N] {
    fn kind() -> Type {
        Type::Array(Box::new(T::kind()), N)
    }
    fn to_value(&self) -> Result<Value> {
        if N > 4095 {
            return Err(ConversionError);
        }
        make(Self::kind(), Data::Sequence(collect(self.iter())?))
    }
}
impl<T: NativeDecode, const N: usize> NativeDecode for [T; N] {
    fn from_value(value: &Value) -> Result<Self> {
        if let Data::Sequence(values) = typed::<Self>(value)? {
            values
                .iter()
                .map(T::from_value)
                .collect::<Result<Vec<_>>>()?
                .try_into()
                .map_err(|_| ConversionError)
        } else {
            Err(ConversionError)
        }
    }
}
impl<'value, T: BorrowDecode<'value>, const N: usize> BorrowDecode<'value> for [T; N] {
    fn from_borrowed(value: &'value Value) -> Result<Self> {
        if let Data::Sequence(values) = typed::<Self>(value)? {
            values
                .iter()
                .map(T::from_borrowed)
                .collect::<Result<Vec<_>>>()?
                .try_into()
                .map_err(|_| ConversionError)
        } else {
            Err(ConversionError)
        }
    }
}
impl<T: StaticTextDecode, const N: usize> StaticTextDecode for [T; N] {
    fn from_static_text(value: &Value, budget: &mut StaticTextBudget) -> Result<Self> {
        if let Data::Sequence(values) = typed::<Self>(value)? {
            values
                .iter()
                .map(|v| T::from_static_text(v, budget))
                .collect::<Result<Vec<_>>>()?
                .try_into()
                .map_err(|_| ConversionError)
        } else {
            Err(ConversionError)
        }
    }
}
impl<T: NativeValue> sealed::Sealed for Option<T> {}
impl<T: NativeValue> NativeValue for Option<T> {
    fn kind() -> Type {
        Type::Option(Box::new(T::kind()))
    }
    fn to_value(&self) -> Result<Value> {
        make(
            Self::kind(),
            match self {
                None => Data::None,
                Some(v) => Data::Some(Box::new(v.to_value()?)),
            },
        )
    }
}
impl<T: NativeDecode> NativeDecode for Option<T> {
    fn from_value(value: &Value) -> Result<Self> {
        match typed::<Self>(value)? {
            Data::None => Ok(None),
            Data::Some(v) => Ok(Some(T::from_value(v)?)),
            _ => Err(ConversionError),
        }
    }
}
impl<'value, T: BorrowDecode<'value>> BorrowDecode<'value> for Option<T> {
    fn from_borrowed(value: &'value Value) -> Result<Self> {
        match typed::<Self>(value)? {
            Data::None => Ok(None),
            Data::Some(v) => Ok(Some(T::from_borrowed(v)?)),
            _ => Err(ConversionError),
        }
    }
}
impl<T: StaticTextDecode> StaticTextDecode for Option<T> {
    fn from_static_text(value: &Value, budget: &mut StaticTextBudget) -> Result<Self> {
        match typed::<Self>(value)? {
            Data::None => Ok(None),
            Data::Some(v) => Ok(Some(T::from_static_text(v, budget)?)),
            _ => Err(ConversionError),
        }
    }
}
impl<T: NativeValue, E: NativeValue> sealed::Sealed for std::result::Result<T, E> {}
impl<T: NativeValue, E: NativeValue> NativeValue for std::result::Result<T, E> {
    fn kind() -> Type {
        Type::Result(Box::new(T::kind()), Box::new(E::kind()))
    }
    fn to_value(&self) -> Result<Value> {
        make(
            Self::kind(),
            match self {
                Ok(v) => Data::Ok(Box::new(v.to_value()?)),
                Err(v) => Data::Err(Box::new(v.to_value()?)),
            },
        )
    }
}
impl<T: NativeDecode, E: NativeDecode> NativeDecode for std::result::Result<T, E> {
    fn from_value(value: &Value) -> Result<Self> {
        match typed::<Self>(value)? {
            Data::Ok(v) => Ok(Ok(T::from_value(v)?)),
            Data::Err(v) => Ok(Err(E::from_value(v)?)),
            _ => Err(ConversionError),
        }
    }
}
impl<'value, T: BorrowDecode<'value>, E: BorrowDecode<'value>> BorrowDecode<'value>
    for std::result::Result<T, E>
{
    fn from_borrowed(value: &'value Value) -> Result<Self> {
        match typed::<Self>(value)? {
            Data::Ok(v) => Ok(Ok(T::from_borrowed(v)?)),
            Data::Err(v) => Ok(Err(E::from_borrowed(v)?)),
            _ => Err(ConversionError),
        }
    }
}
impl<T: StaticTextDecode, E: StaticTextDecode> StaticTextDecode for std::result::Result<T, E> {
    fn from_static_text(value: &Value, budget: &mut StaticTextBudget) -> Result<Self> {
        match typed::<Self>(value)? {
            Data::Ok(v) => Ok(Ok(T::from_static_text(v, budget)?)),
            Data::Err(v) => Ok(Err(E::from_static_text(v, budget)?)),
            _ => Err(ConversionError),
        }
    }
}
impl sealed::Sealed for () {}
impl NativeValue for () {
    fn kind() -> Type {
        Type::Tuple(vec![])
    }
    fn to_value(&self) -> Result<Value> {
        make(Self::kind(), Data::Tuple(vec![]))
    }
}
impl NativeDecode for () {
    fn from_value(value: &Value) -> Result<Self> {
        typed::<Self>(value).map(|_| ())
    }
}
impl<'value> BorrowDecode<'value> for () {
    fn from_borrowed(value: &'value Value) -> Result<Self> {
        Self::from_value(value)
    }
}
impl StaticTextDecode for () {
    fn from_static_text(value: &Value, _budget: &mut StaticTextBudget) -> Result<Self> {
        Self::from_value(value)
    }
}
macro_rules! tuple {
    ($($ty:ident:$idx:tt),+) => {
        impl<$($ty: NativeValue),+> sealed::Sealed for ($($ty,)+) {}
        impl<$($ty: NativeValue),+> NativeValue for ($($ty,)+) {
            fn kind() -> Type { Type::Tuple(vec![$($ty::kind()),+]) }
            fn to_value(&self) -> Result<Value> { make(Self::kind(), Data::Tuple(vec![$(self.$idx.to_value()?),+])) }
        }
        impl<$($ty: NativeDecode),+> NativeDecode for ($($ty,)+) {
            fn from_value(value: &Value) -> Result<Self> {
                if let Data::Tuple(values) = typed::<Self>(value)? { Ok(($($ty::from_value(&values[$idx])?,)+)) }
                else { Err(ConversionError) }
            }
        }
        impl<'value,$($ty: BorrowDecode<'value>),+> BorrowDecode<'value> for ($($ty,)+) {
            fn from_borrowed(value: &'value Value) -> Result<Self> {
                if let Data::Tuple(values)=typed::<Self>(value)? { Ok(($($ty::from_borrowed(&values[$idx])?,)+)) } else { Err(ConversionError) }
            }
        }
        impl<$($ty:StaticTextDecode),+> StaticTextDecode for ($($ty,)+) {
            fn from_static_text(value:&Value,budget:&mut StaticTextBudget)->Result<Self>{
                if let Data::Tuple(values)=typed::<Self>(value)? {Ok(($($ty::from_static_text(&values[$idx],budget)?,)+))}else{Err(ConversionError)}
            }
        }
    }
}
tuple!(A:0);
tuple!(A:0,B:1);
tuple!(A:0,B:1,C:2);
tuple!(A:0,B:1,C:2,D:3);
tuple!(A:0,B:1,C:2,D:3,E:4);
tuple!(A:0,B:1,C:2,D:3,E:4,F:5);
tuple!(A:0,B:1,C:2,D:3,E:4,F:5,G:6);
tuple!(A:0,B:1,C:2,D:3,E:4,F:5,G:6,H:7);
tuple!(A:0,B:1,C:2,D:3,E:4,F:5,G:6,H:7,I:8);
tuple!(A:0,B:1,C:2,D:3,E:4,F:5,G:6,H:7,I:8,J:9);
tuple!(A:0,B:1,C:2,D:3,E:4,F:5,G:6,H:7,I:8,J:9,K:10);
tuple!(A:0,B:1,C:2,D:3,E:4,F:5,G:6,H:7,I:8,J:9,K:10,L:11);
tuple!(A:0,B:1,C:2,D:3,E:4,F:5,G:6,H:7,I:8,J:9,K:10,L:11,M:12);
tuple!(A:0,B:1,C:2,D:3,E:4,F:5,G:6,H:7,I:8,J:9,K:10,L:11,M:12,N:13);
tuple!(A:0,B:1,C:2,D:3,E:4,F:5,G:6,H:7,I:8,J:9,K:10,L:11,M:12,N:13,O:14);
tuple!(A:0,B:1,C:2,D:3,E:4,F:5,G:6,H:7,I:8,J:9,K:10,L:11,M:12,N:13,O:14,P:15);

pub fn referent(value: &Value) -> Result<&Value> {
    if let Data::Ref(v) = value.data() {
        Ok(v)
    } else {
        Err(ConversionError)
    }
}
pub fn decode_slice<T: NativeDecode>(value: &Value) -> Result<Vec<T>> {
    if value.kind() != &Type::Slice(Box::new(T::kind())) {
        return Err(ConversionError);
    }
    if let Data::Sequence(values) = value.data() {
        values.iter().map(T::from_value).collect()
    } else {
        Err(ConversionError)
    }
}
pub fn decode_borrowed_slice<'value, T: BorrowDecode<'value>>(
    value: &'value Value,
) -> Result<Vec<T>> {
    if value.kind() != &Type::Slice(Box::new(T::kind())) {
        return Err(ConversionError);
    }
    if let Data::Sequence(values) = value.data() {
        values.iter().map(T::from_borrowed).collect()
    } else {
        Err(ConversionError)
    }
}
pub fn decode_static_slice<T: StaticTextDecode>(
    value: &Value,
    budget: &mut StaticTextBudget,
) -> Result<Vec<T>> {
    if value.kind() != &Type::Slice(Box::new(T::kind())) {
        return Err(ConversionError);
    }
    if let Data::Sequence(values) = value.data() {
        values
            .iter()
            .map(|v| T::from_static_text(v, budget))
            .collect()
    } else {
        Err(ConversionError)
    }
}

#[cfg(test)]
mod static_tests {
    use super::*;
    #[test]
    fn static_text_budget_bounds_allocations_before_copy() {
        let mut budget = StaticTextBudget {
            bytes: 3,
            strings: 2,
        };
        assert_eq!(budget.copy("abc").unwrap(), "abc");
        assert!(budget.copy("x").is_err());
        assert_eq!(budget.copy("").unwrap(), "");
        assert!(budget.copy("").is_err());
    }
}
